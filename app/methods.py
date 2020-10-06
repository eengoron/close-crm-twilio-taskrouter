import json
import logging
import os

import flask
from closeio_api import Client as CloseIO_API
from flask import Response
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse

# Format Logging
log_format = "[%(asctime)s] %(levelname)s %(message)s"
logging.basicConfig(level=logging.INFO, format=log_format)
twilio_logger = logging.getLogger('twilio')
twilio_logger.setLevel(logging.ERROR)

SITE_ROOT = os.path.realpath(os.path.dirname(__file__))
json_url = os.path.join(SITE_ROOT, "static/", "config.json")
with open(json_url) as f:
    config = json.load(f)

hold_music_url = os.path.join(
    SITE_ROOT, "static/", config['hold_music_filename']
)

# Initialize Close Variables
api = CloseIO_API(os.environ.get('CLOSE_API_KEY'))
org_id = api.get('api_key/' + os.environ.get('CLOSE_API_KEY'))[
    'organization_id'
]

# Initialize the Twilio API
twilio_client = Client(
    os.environ.get('TWILIO_ACCOUNT_SID'), os.environ.get('TWILIO_AUTH_TOKEN')
)
workspace_sid = os.environ.get('TWILIO_WORKSPACE_SID')
workflow_sid = os.environ.get('TWILIO_WORKFLOW_SID')

# The Base URL of the Application
base_url = os.environ.get('BASE_URL')

#######
# Twilio
#######


def twiml(resp):
    """Helper method to wrap TwiML."""
    resp = flask.Response(str(resp))
    resp.headers['Content-Type'] = 'text/xml'
    return resp


def _fetch_worker_sid_to_worker_attributes_map():
    """
    Return a dictionary of Twilio Worker SIDs to useful attributes about the
    worker:
        friendly_name: The name of the Worker
        activity_name: The current activity name the Worker has in Twilio. This
            is the equivalent of the user's availability in Close and can have a
            a value of online, offline, or on_call.
        close_user_id: The Close User ID of the Twilio Worker.
        groups: A list of groups that the Twilio Worker is a part of, taken
        from Close.
    """
    worker_sid_to_attributes_map = {}
    try:
        all_workers = twilio_client.taskrouter.workspaces(
            workspace_sid
        ).workers.list()
        for worker in all_workers:
            worker_data = {
                'friendly_name': worker.friendly_name,
                'activity_name': worker.activity_name,
            }
            attributes = json.loads(worker.attributes)
            if attributes.get('close_user_id'):
                worker_data.update(attributes)
            worker_sid_to_attributes_map[worker.sid] = worker_data
    except Exception as e:
        logging.error(
            f"Failed to fetch a worker sid to attributes map because {str(e)}"
        )

    return worker_sid_to_attributes_map


def _fetch_queue_by_twilio_number(twilio_number):
    """
    Fetches a queue mapping by twilio_number.

    Args:
        twilio_number: The number you want to search for in the list of queues

    Returns:
        dict: The queue config for that Twilio number, or None if it does not
        exist.
    """
    try:
        return [
            i
            for i in config['queue_mappings']
            if i['twilio_number'] == twilio_number
        ][0]
    except Exception:
        logging.error(f'A queue for {twilio_number} does not exist')
        return None

    return None


def update_twilio_worker_status(worker_sid, new_status):
    """
    Update a given Twilio Worker's status in Twilio to a new status.

    The status can either be:
     - online
     - offline
     - on_call

    Given the friendly name of the new status, we use the twilio_status_mapping
    config to get the activity_sid, and PUT that onto the worker.

    Args:
        worker_sid (str): The worker's SID in Twilio
        new_status (str): The friendly_name of the worker's new status in Twilio
    """
    try:
        activity_sid = config['twilio_status_mapping'].get(new_status)
        if activity_sid:
            twilio_client.taskrouter.workspaces(workspace_sid).workers(
                worker_sid
            ).update(activity_sid=activity_sid)
        else:
            logging.error(
                f"Failed when updating the status of {worker_sid} to {new_status} because the status does not exist"
            )
    except Exception as e:
        logging.error(
            f"Failed when updating the status of {worker_sid} to {new_status} because {str(e)}"
        )


def update_twilio_worker_groups_attribute(worker_sid, close_user_id, groups):
    """
    Update a given Twilio Worker's groups attribute to a new list of groups.

    Args:
        worker_sid (str): The worker's SID in Twilio
        close_user_id (str): The Close User ID of the current worker
        groups (list): The list of user manager groups that this Worker is
        currently a part of in Close.
    """
    try:
        attributes = {'close_user_id': close_user_id, 'groups': groups}
        attributes = json.dumps(attributes)
        twilio_client.taskrouter.workspaces(workspace_sid).workers(
            worker_sid
        ).update(attributes=attributes)
    except Exception as e:
        logging.error(
            f"Failed updating {worker_sid}'s groups attribute because {str(e)}"
        )


def create_twilio_worker(close_user_id, user_name):
    """
    Create a new Twilio Worker.

    Args:
        close_user_id (str): The user ID of the newly added user.
        user_name (str): The name of the newly added user.
    """
    try:
        attributes = {'close_user_id': close_user_id, 'groups': []}
        twilio_client.taskrouter.workspaces(workspace_sid).workers.create(
            friendly_name=user_name, attributes=json.dumps(attributes)
        )
    except Exception as e:
        logging.error(
            f"Failed to create a new Twilio worker with name {user_name} and user_id {close_user_id} because {str(e)}"
        )
        return str(e)


def remove_twilio_worker_by_worker_sid(worker_sid):
    """
    Removes a Twilio worker.

    Args:
        worker_sid: The SID of the worker being removed.
    """
    try:
        twilio_client.taskrouter.workspaces(workspace_sid).workers(
            worker_sid
        ).delete()
    except Exception as e:
        logging.error(
            f"Failed to delete a twilio worker with worker_sid {worker_sid} because {str(e)}"
        )
        return str(e)


def check_for_online_users_based_on_twilio_phone(phone):
    """
    Check whether or not all users assigned to a specific TaskQueue are offline.
    If they are, we just forward to the group number in Close so that they can
    leave a voicemail, and bypass the queue completely.

    We do this instead of creating a "Queue" for Voicemail in Twilio because
    we want the voicemail to be logged in Close.

    Args:
        phone (str): The phone number of the Twilio queue dialed into

    Returns:
        bool: True if users are online, false if all users are offline.
    """
    try:
        queue_for_number = _fetch_queue_by_twilio_number(phone)
        if not queue_for_number:
            logging.error(
                f'Could not check for Online users because a queue for {phone} does not exist.'
            )
            return False

        group_id_for_queue = queue_for_number['close_user_manager_group_id']
        twilio_workers = _fetch_worker_sid_to_worker_attributes_map()
        for worker_sid, attributes in twilio_workers.items():
            if attributes.get('close_user_id') and attributes.get('groups'):
                if (
                    group_id_for_queue in attributes['groups']
                    and attributes['activity_name'] != 'offline'
                ):
                    return True
    except Exception as e:
        logging.error(
            f"Failed when checking to see if any users were online for {phone} because {str(e)}"
        )
        return False

    return False


def mark_twilio_task_as_done_when_assigned(task_sid):
    """
    Mark a Twilio task as "completed" as soon as it's assigned assigned.

    The reason we do this is because we have to use the redirect verb with
    enqueue so that the originators Caller ID will be correctly reflected in
    Close.

    However, once a redirect occurs, the task remains in the TaskQueue, so we
    need to complete it to make users "online"/"available" to accept new calls
    again.

    Args:
        task_sid (str): The ID of the task in Twilio we want to mark as
            complete.
    """
    try:
        twilio_client.taskrouter.workspaces(workspace_sid).tasks(
            task_sid
        ).update(assignment_status='completed')
    except Exception as e:
        logging.error(
            f"Failed to mark task {task_sid} as complete because {str(e)}"
        )


def send_call_to_queue(request):
    """
    Queue a Twilio call based on the number (queue) that was called. Before we
    use the enqueue verb, we double check to make sure someone is online.

    If no one is online, we completely bypass the queue and let the user
    leave a voicemail ASAP.

    If the user is waiting in the queue, they have the option to press any key
    to leave a voicemail at any time. If they choose to leave a voicemail,
    they will be redirected to a Close fallback number that goes directly to
    voicemail.
    """
    response = VoiceResponse()
    try:
        to_number = request.values.get('To')
        queue = _fetch_queue_by_twilio_number(to_number)
        # If the number doesn't exist in any queue, return early and use the
        # fallback number. This should never happen, but is there just in case.
        if not queue:
            response.dial(config['fallback_number'])
            logging.error(
                f"The fallback number was used to redirect a call because {to_number} is not a real queue."
            )

        # If no one is online, but the queue exists dial the number directly so
        # that the caller can leave a voicemail.
        if (
            not check_for_online_users_based_on_twilio_phone(to_number)
            and queue
        ):
            response.dial(queue['close_group_number'])

        enqueue = response.enqueue(
            None, workflow_sid=workflow_sid, wait_url='/wait-url/'
        )
        enqueue.task(json.dumps({'to_number': to_number}))
        response.dial(config['fallback_number'])
        response.append(enqueue)
        return twiml(response)
    except Exception as e:
        logging.error(
            f"Failed to correctly send a call to the correct desination because {str(e)}"
        )


def send_redirect_instruction_on_assignment_callback(task_id, task_attributes):
    """
    Send the redirect instruction to Twilio when calls are assigned so that when
    calls come into the Close group number they display thecaller ID of the
    originator.
    """
    try:
        queue = _fetch_queue_by_twilio_number(task_attributes.get('to_number'))

        # We redirect because otherwise Close won't display the right value for the incoming caller
        response = {
            'instruction': 'redirect',
            'call_sid': task_attributes.get('call_sid'),
            'url': f"{os.environ.get('BASE_URL')}redirect-task/?task_id={task_id}&phone_number={queue['close_group_number']}",
            'accept': True,
        }

        resp = Response(
            response=json.dumps(response),
            status=200,
            mimetype='application/json',
        )
        return resp
    except Exception as e:
        logging.error(
            f"Failed when redirecting a call to a Close group number because {str(e)}"
        )
        return str(e)


def dial_redirected_phone_number(request):
    """
    Handle the redirect instruction when it's sent on the assignment callback
    above.

    This method takes the phone_number that will be dialed and the task_id from
    the URL, marks the task as "complete" so that it doesn't remain in the queue
    after it redirects, and then calls the appropriate number.
    """
    try:
        phone_number = request.args.get('phone_number')
        task_id = request.args.get('task_id')
        if task_id:
            mark_twilio_task_as_done_when_assigned(task_id)
        response = VoiceResponse()
        if phone_number:
            response.dial(phone_number)
        else:
            response.dial()
        return twiml(response)
    except Exception as e:
        logging.error(
            f"Failed when processing the redirect instruction for {phone_number} on {task_id} because {str(e)}"
        )


def setup_wait_url():
    """
    Setup the Wait URL in Twilio so that we give the user the option to leave
    the queue at any time and we also play a predetermined audio-file for hold
    music.
    """
    response = VoiceResponse()
    with response.gather(
        num_digits=1, action="/forward-to-vm/", method="POST"
    ) as g:
        g.play(hold_music_url)
    return twiml(response)


def redirect_key_press_to_vm(request):
    """Redirect to voicemail on keypress."""
    response = VoiceResponse()
    response.leave()
    return twiml(response)


#######
# Close
#######


def ensure_all_memberships_have_workers():
    """
    Make sure that every single active Close user in the given organization
    has a Twilio worker.
    """
    twilio_workers = _fetch_worker_sid_to_worker_attributes_map()
    existing_close_user_ids = {
        attributes['close_user_id'] for k, attributes in twilio_workers.items()
    }
    try:
        memberships = api.get(
            'organization/' + org_id, params={'_fields': 'memberships'}
        )['memberships']
        for membership in memberships:
            if membership['user_id'] not in existing_close_user_ids:
                create_twilio_worker(
                    membership['user_id'], membership['user_full_name']
                )
        return True
    except Exception as e:
        logging.error(
            f"Failed when checking for Twilio workers on startup because {str(e)}"
        )


def process_close_group_update(group_id, group_members):
    """
    Process group updates by making sure Twilio Workers are in order.

    When a new members update comes in for groups involved in a queue,
    ensure that every user has a Twilio worker and that statuses in Twilio are
    correct.
    """
    try:
        if group_id not in [
            i['close_user_manager_group_id'] for i in config['queue_mappings']
        ]:
            return False

        # On any group update that matters, make sure everyone's Twilio workers
        # are in order.
        ensure_all_memberships_have_workers()
        update_all_twilio_statuses_and_group_number_participants()
    except Exception as e:
        logging.error(
            f'Failed to proccess Close group update for {group_id} because {str(e)}'
        )


def _fetch_user_id_to_close_availability_map():
    """
    Return a dictionary of User ID to availability status in Close. The
    possible values are:
     - online: The user is online in the native application
     - offline: The user is offline in the native application
     - on_call: The user is currently on a call in Close
    """
    user_availability_map = {}
    try:
        current_availability = api.get(
            'user/availability', params={'organization_id': org_id}
        )
        for user in current_availability['data']:
            native_app_availability = [
                i for i in user['availability'] if i['type'] == 'native'
            ][0]
            status = native_app_availability.get('status', 'offline')
            if native_app_availability.get('active_calls', []):
                status = 'on_call'
            user_availability_map[user['user_id']] = status
    except Exception as e:
        logging.error(f'Could not pull user availability map because {str(e)}')
    return user_availability_map


def _fetch_group_id_group_users_map():
    """
    Returns a dictionary of group_id to list of user_ids currently in that
    group.

    We get our group_id list from config.json.
    """
    group_members_mapping = {}
    try:
        groups = [
            i['close_user_manager_group_id'] for i in config['queue_mappings']
        ]
        for group in groups:
            resp = api.get(f'group/{group}', params={'_fields': 'members'})[
                'members'
            ]
            group_members_mapping[group] = [i['user_id'] for i in resp]
    except Exception as e:
        logging.error(f'Could not pull groups to users map because {str(e)}')
    return group_members_mapping


def update_groups_attribute_for_twilio_workers_from_list_of_users_in_close_groups(
    groups_to_users_map=None, twilio_workers=None
):
    """
    Pull a list of users for the user manager groups (listed in config.js) from
    Close and make sure that each user in each group has that group attribute
    listed on their Twilio Workers.
    """
    try:
        twilio_workers = (
            twilio_workers or _fetch_worker_sid_to_worker_attributes_map()
        )
        groups_to_users_map = (
            groups_to_users_map or _fetch_group_id_group_users_map()
        )
        close_user_ids_to_twilio_worker_ids = {
            attributes['close_user_id']: k
            for k, attributes in twilio_workers.items()
        }
        twilio_worker_to_group_mappings = {
            k: [] for k, attributes in twilio_workers.items()
        }
        for group, user_ids in groups_to_users_map.items():
            for user_id in user_ids:
                worker_id = close_user_ids_to_twilio_worker_ids.get(
                    user_id, None
                )
                if worker_id:
                    twilio_worker_to_group_mappings[worker_id].append(group)

        for k, attributes in twilio_workers.items():
            worker_groups = twilio_worker_to_group_mappings.get(k, [])
            if not attributes.get('groups') or sorted(
                attributes.get('groups', [])
            ) != sorted(worker_groups):
                update_twilio_worker_groups_attribute(
                    k, attributes['close_user_id'], worker_groups
                )
    except Exception as e:
        logging.error(
            f"Failed to update groups attribute for Twilio Workers from a Close list because {str(e)}"
        )


def delete_twilio_worker_from_close_user_id(user_id):
    """
    Delete a Twilio worker when a user is deactivated in Close.

    We first have to make the user offline, just in case the Availability
    endpoint hasn't refreshed yet.

    Args:
        user_id: The user_id of the User that was deactivated in Close
    """
    try:
        twilio_workers = _fetch_worker_sid_to_worker_attributes_map()
        for worker_sid, attributes in twilio_workers.items():
            if attributes['close_user_id'] == user_id:
                update_twilio_worker_status(worker_sid, 'offline')
                remove_twilio_worker_by_worker_sid(worker_sid)
    except Exception as e:
        logging.error(
            f"Failed to delete worker for {user_id} because {str(e)}"
        )
        return str(e)


def update_close_group_number_participants_from_availability(
    user_availability_map=None, groups_to_users_map=None
):
    """
    Update Close group number participants for each queue based on the current
    availability of each User in Close.
    """
    try:
        user_availability_map = (
            user_availability_map or _fetch_user_id_to_close_availability_map()
        )
        groups_to_users_map = (
            groups_to_users_map or _fetch_group_id_group_users_map()
        )
        for queue in config['queue_mappings']:
            user_ids_in_group = groups_to_users_map.get(
                queue['close_user_manager_group_id'], []
            )
            expected_participants = [
                user_id
                for user_id in user_ids_in_group
                if user_availability_map.get(user_id, 'offline') == 'online'
            ]
            participants_currently_in_close = api.get(
                f"phone_number/{queue['close_group_number_id']}",
                params={'_fields': 'participants'},
            )['participants']
            if sorted(expected_participants) != sorted(
                participants_currently_in_close
            ):
                api.put(
                    f"phone_number/{queue['close_group_number_id']}",
                    data={'participants': expected_participants},
                )
    except Exception as e:
        logging.error(
            f"Failed to update Close group number participants because {str(e)}"
        )


def update_twilio_worker_statuses_from_close_status(
    user_availability_map=None, twilio_workers=None
):
    """
    Update every Twilio worker's status based on Close availability.

    In order to do this, we first get the the availability from every user in
    Close. Then, we get the list of Twilio workers so we can match Close User ID
    to Twilio worker SID. Then we get update the status of each Twilio worker
    that doesn't match their respective Close Status.
    """
    try:
        user_availability_map = (
            user_availability_map or _fetch_user_id_to_close_availability_map()
        )
        twilio_workers = (
            twilio_workers or _fetch_worker_sid_to_worker_attributes_map()
        )
        for worker_sid, twilio_attributes in twilio_workers.items():
            user_id = twilio_attributes.get('close_user_id', None)
            if user_id:
                user_status_in_close = user_availability_map.get(
                    user_id, 'offline'
                )
                if twilio_attributes['activity_name'] != user_status_in_close:
                    update_twilio_worker_status(
                        worker_sid, user_status_in_close
                    )
    except Exception as e:
        logging.error(
            f"Failed to update Twilio worker statuses by Close availability because {str(e)}"
        )


def update_all_twilio_statuses_and_group_number_participants():
    """
    Updates Twilio Worker Status and Close Group Number participants based on
    current availability status in Close.
    """
    close_availability = _fetch_user_id_to_close_availability_map()
    group_users = _fetch_group_id_group_users_map()
    twilio_workers = _fetch_worker_sid_to_worker_attributes_map()
    update_twilio_worker_statuses_from_close_status(
        user_availability_map=close_availability, twilio_workers=twilio_workers
    )
    update_close_group_number_participants_from_availability(
        user_availability_map=close_availability,
        groups_to_users_map=group_users,
    )
    update_groups_attribute_for_twilio_workers_from_list_of_users_in_close_groups(
        groups_to_users_map=group_users, twilio_workers=twilio_workers
    )


ensure_all_memberships_have_workers()
update_all_twilio_statuses_and_group_number_participants()
