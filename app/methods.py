from closeio_api import Client as CloseIO_API, APIError
import os
import logging
from twilio.rest import Client
import json
import flask
from flask import Response, url_for
from twilio.twiml.voice_response import VoiceResponse, Play

## Format Logging
log_format = "[%(asctime)s] %(levelname)s %(message)s"
logging.basicConfig(level=logging.INFO, format=log_format)
twilio_logger = logging.getLogger('twilio')
twilio_logger.setLevel(logging.ERROR)

## Initiate Close API
api = CloseIO_API(os.environ.get('CLOSE_API_KEY'))
org_id = api.get('api_key/' + os.environ.get('CLOSE_API_KEY'))['organization_id']

## Initialize Close Variables
close_user_ids_to_twilio_worker_ids = {}
close_user_ids_to_current_calls = {}
close_user_ids_to_twilio_phone_numbers = {}
close_phone_numbers = os.environ.get('CLOSE_PHONE_NUMBERS').split(',')

## Initiate the Twilio API
twilio_client = Client(os.environ.get('TWILIO_ACCOUNT_SID'), os.environ.get('TWILIO_AUTH_TOKEN'))
workspace_sid = os.environ.get('TWILIO_WORKSPACE_SID')
workflow_sid = os.environ.get('TWILIO_WORKFLOW_SID')

## Initialize Twilio Varaibles
twilio_phone_numbers = os.environ.get('TWILIO_PHONE_NUMBERS').split(',')
twilio_statuses = {}
twilio_worker_ids_to_close_user_ids = {}
twilio_workers_availability_status = {}
twilio_to_close_phone_mapping = {}
twilio_phone_numbers_to_queue_id = {}

## Other Environmental Variables
base_url = os.environ.get('BASE_URL') ## The Base URL of the Application
filename = os.environ.get('FILENAME') ## The filename of the hold music used in the queues
fallback_number = os.environ.get('FALLBACK_NUMBER') ## The fallback number used when Close needs to leave a voicemail or if something goes wrong.

## Generate Twilio to Close phone mapping
for x in range(0, len(twilio_phone_numbers)):
    twilio_to_close_phone_mapping[twilio_phone_numbers[x]] = close_phone_numbers[x]

##########################
# Twilio
##########################

## TwiML conversion helper when returning a response from one of the methods below
def twiml(resp):
    resp = flask.Response(str(resp))
    resp.headers['Content-Type'] = 'text/xml'
    return resp

## Method to get the IDs of possible Twilio Worker statuses. In this case, we only use
## the default Twilio worker statuses of "Offline", "Available", and "Unavailable" because those are the only three options
## we need. With this being said, This method gets the SIDs of "Offline", "Available", and "Unavailable" for the specific
## Twilio account because we need them to update the statuses of workers when new calls come in.
def get_twilio_worker_statuses():
    try:
        activities = twilio_client.taskrouter.workspaces(workspace_sid).activities.list()
        twilio_statuses.update({ i.friendly_name : i.sid for i in activities })
        return twilio_statuses
    except Exception as e:
        logging.error(f"Failed when getting possible worker activities because {str(e)}")
        return str(e)

## Method to get a list of all Twilio workers for a particular workspace and make the corresponding
## mapping array between Close users and Twilio Worker IDs. Additionally, this method also
## creates a mapping of Twilio Workers with a "close_user_id" attribute in Twilio and Close group numbers to be used
## when updating potential group numbers for a Close user.
def get_twilio_workers():
    try:
        all_workers = twilio_client.taskrouter.workspaces(workspace_sid).workers.list()
        for worker in all_workers:
            attributes = json.loads(worker.attributes)
            if attributes.get('close_user_id'):
                close_user_ids_to_twilio_worker_ids[attributes['close_user_id']] = worker.sid
                twilio_worker_ids_to_close_user_ids[worker.sid] = attributes['close_user_id']
                twilio_workers_availability_status[worker.sid] = worker.activity_name
            if attributes.get('group_phones'):
                close_user_ids_to_twilio_phone_numbers[attributes['close_user_id']] = attributes['group_phones']
        return True
    except Exception as e:
        logging.error(f"Failed when getting all twilio workers and making the corresponding mapping arrays because {str(e)}")
        return str(e)

## Method to generate a mapping from Twilio Phone numbers to Twilio Queue IDs to be used when forwarding a number or decding
## if a call should just go to the fallback number.
def get_twilio_taskrouter_queues():
    try:
        all_queues = twilio_client.taskrouter.workspaces(workspace_sid).task_queues.list()
        twilio_phone_numbers_to_queue_id.update({ i.friendly_name : i.sid for i in all_queues if i.friendly_name in twilio_phone_numbers })
        return twilio_phone_numbers_to_queue_id
    except Exception as e:
        logging.error(f"Failed when getting possible twilio task router queues because {str(e)}")
        return str(e)

## Method to update Twilio worker's status by worker_id. This method is used when users log into and out of close
## and when calls come into and out of Close to update the workers status in Twilio.
def update_twilio_worker_status(worker_sid, new_status):
    try:
        if twilio_statuses.get(new_status):
            twilio_client.taskrouter.workspaces(workspace_sid).workers(worker_sid).update(activity_sid=twilio_statuses[new_status])
            twilio_workers_availability_status[worker_sid] = new_status
            return new_status
        else:
            logging.error(f"Failed when updating the status of {worker_sid} to {new_status} because the status does not exist")
    except Exception as e:
        logging.error(f"Failed when updating the status of {worker_sid} to {new_status} because {str(e)}")
        return str(e)

## Method to update Twilio worker attributes by worker_sid. Used for when updating phone number memberships
def update_worker_phone_numbers_attribute(worker_sid, phone_numbers):
    try:
        close_user_id = twilio_worker_ids_to_close_user_ids.get(worker_sid)
        if close_user_id:
            attributes = { 'close_user_id' : close_user_id, 'group_phones': phone_numbers }
            attributes = json.dumps(attributes)
            twilio_client.taskrouter.workspaces(workspace_sid).workers(worker_sid).update(attributes=attributes)
            close_user_ids_to_twilio_phone_numbers[close_user_id] = phone_numbers
        return True
    except Exception as e:
        logging.error(f"Failed updating {worker_sid}'s phone number attribute because {str(e)}")
        return str(e)

## Method to check whether or not all users assigned to a specific TaskQueue are offline. If they are, we just forward to the group number in Close
## so that they can leave a voicemail, and bypass the queue completely. We do this instead of creating a "Queue" for Voicemail in Twilio because
## we want the voicemail to be logged in Close.
def check_for_online_users_based_on_twilio_phone(phone):
    try:
        close_users = [i for i in list(close_user_ids_to_twilio_phone_numbers.keys()) if phone in close_user_ids_to_twilio_phone_numbers[i]]
        for user in close_users:
            twilio_worker_id = close_user_ids_to_twilio_worker_ids.get(user)
            if twilio_worker_id and twilio_workers_availability_status.get(twilio_worker_id, "Offline") in ['Available', 'Unavailable']:
                return True
        return False
    except Exception as e:
        logging.error(f"Failed when checking to see if any users were online for {phone} because {str(e)}")
        return False


## Method to create a Twilio worker when a new user is added into Close based on the membership.activated event
def create_twilio_worker(close_user_id, user_name):
    try:
        attributes = { 'close_user_id': close_user_id }
        new_worker = twilio_client.taskrouter.workspaces(workspace_sid).workers.create(friendly_name=user_name, attributes=json.dumps(attributes))
        twilio_worker_ids_to_close_user_ids[new_worker.sid] = close_user_id
        close_user_ids_to_twilio_worker_ids[close_user_id] = new_worker.sid
        twilio_workers_availability_status[new_worker.sid]  = "Offline"
        update_close_availability()
        return new_worker
    except Exception as e:
        logging.error(f"Failed to create a new Twilio worker with name {user_name} and user_id {close_user_id} because {str(e)}")
        return str(e)

## Method to delete a worker when a membership is deactivated in Close based on the membership.deactivated event
## If a user is removed and then readded, the worker will be recreated.
def remove_twilio_worker_by_worker_sid(worker_sid):
    try:
        twilio_client.taskrouter.workspaces(workspace_sid).workers(worker_sid).delete()
    except Exception as e:
        logging.error(f"Failed to delete a twilio worker with worker_sid {worker_sid} because {str(e)}")
        return str(e)

## Method to mark a Twilio task as "completed" as soon as it's assigned assigned. The reason we do this is because
## we have to use the redirect verb with enqueue so that the originators Caller ID will be correctly reflected in Close.
## However, once a redirect occurs, the task remains in the TaskQueue, so we need to complete it to make users "Available" to accept
## new calls again.
def mark_twilio_task_as_done_when_assigned(task_sid):
    try:
        twilio_client.taskrouter.workspaces(workspace_sid).tasks(task_sid).update(assignment_status='completed')
        return True
    except Exception as e:
        logging.error(f"Failed to mark task {task_sid} as complete because {str(e)}")
        return str(e)

## Method for correctly queuing a Twilio call based on the number that they called into. Before we use the enqueue verb,
## we double check to make sure someone is online, so that if no one is online, we can completely bypass the queue and let
## the user leave a voicemail ASAP. If the user is waiting in the queue, they have the option to press any key to leave a voicemail at any time.
## If they choose to leave a voicemail, they will be redirected to a Close fallback number that goes directly to voicemail.
def send_call_to_queue(request):
    try:
        to_number = request.values.get('To')
        if to_number and to_number in list(twilio_phone_numbers_to_queue_id.keys()):
            response = VoiceResponse()
            if check_for_online_users_based_on_twilio_phone(to_number):
                enqueue = response.enqueue(None, workflow_sid=workflow_sid, wait_url='/wait-url/')
                enqueue.task(json.dumps({ 'to_number': to_number }))
                response.dial(fallback_number)
                response.append(enqueue)
            else:
                if twilio_to_close_phone_mapping.get(to_number):
                    response.dial(twilio_to_close_phone_mapping[to_number])
                else:
                    response.dial(fallback_number)
                    logging.error(f"The fallback number was used to redirect a call because {to_number} did not exist in the Twilio > Close Phone Mapping.")
        return twiml(response)
    except Exception as e:
        logging.error(f"Failed to correctly send a call to the correct desination because {str(e)}")
        return str(e)

## Method for sending the redirect instruction to Twilio when calls are assigned so that when calls come into the Close group number
## they display thecaller ID of the originator
def send_redirect_instruction_on_assignment_callback(task_id, task_attributes):
    try:
        ## We redirect because otherwise Close won't display the right value for the incoming caller
        response = {
            'instruction': 'redirect',
            'call_sid': task_attributes.get('call_sid'),
            'url': f"{os.environ.get('BASE_URL')}redirect-task/?task_id={task_id}&phone_number={twilio_to_close_phone_mapping[task_attributes.get('to_number')]}",
            'accept': True
        }

        resp = Response(response=json.dumps(response), status=200, mimetype='application/json')
        return resp
    except Exception as e:
        logging.error(f"Failed when redirecting a call to a Close group number because {str(e)}")
        return str(e)

## Method for handling the redirect instruction when it's sent on the assignment callback above. This method takes the phone_number that will be dialed
## and the task_id from the URL, marks the task as "complete" so that it doesn't remain in the queue after it redirects, and then calls the appropriate number.
def dial_redirected_phone_number(request):
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
        logging.error(f"Failed when processing the redirect instruction for {phone_number} on {task_id} because {str(e)}")
        return str(e)

## Method for setting up the Wait URL in Twilio so that we give the user the option to leave the queue at any time and
## we also play a predetermined audio-file for hold music.
def setup_wait_url():
    response = VoiceResponse()
    with response.gather(num_digits=1, action="/forward-to-vm/", method="POST") as g:
        g.say("Thanks for calling in,,,,,,Press any key at any time to exit the queue and be redirected to a voicemail box.")
        g.play(url_for('static', filename=filename))
    return twiml(response)

## Method to redirect on a keypress while waiting in a TaskQueue to leave a Voicemail on the fallback number.
def redirect_key_press_to_vm(request):
    response = VoiceResponse()
    response.leave()
    return twiml(response)

##########################
# Close
##########################

## Method to make sure that when the application starts up, all active Close users have a Worker in Twilio.
## If not, a worker is created for them.
def make_sure_all_memberships_have_workers_on_startup():
    try:
        memberships = api.get('organization/' + org_id, params={ '_fields': 'memberships'})['memberships']
        for membership in memberships:
            if membership['user_id'] not in close_user_ids_to_twilio_worker_ids:
                create_twilio_worker(membership['user_id'], membership['user_full_name'])
        return True
    except Exception as e:
        logging.error(f"Failed when checking for Twilio workers on startup because {str(e)}")
        return str(e)

## Method to update Close user availability in Twilio based on login, logouts as well as incoming and outgoing phone calls that are picked up.
## This method runs on a schedule to consistently poll the Close API for new availability information.
def update_close_availability():
    try:
        current_close_availability = api.get('user/availability', params={ 'organization_id': org_id })
        for user in current_close_availability['data']:
            if close_user_ids_to_twilio_worker_ids.get(user['user_id']):
                twilio_worker_id = close_user_ids_to_twilio_worker_ids.get(user['user_id'])
                if twilio_worker_id:
                    native_availability = [i['status'] == 'online' for i in user['availability'] if i['type'] == 'native'][0]
                    new_twilio_status = 'Offline' if not native_availability else "Available"
                    if new_twilio_status == 'Available' and len(close_user_ids_to_current_calls.get(user['user_id'], [])) != 0:
                        new_twilio_status = 'Unavailable'
                    if twilio_workers_availability_status.get(twilio_worker_id) != new_twilio_status:
                        update_twilio_worker_status(twilio_worker_id, new_twilio_status)

        ## We also check each phone number to see if there have been any phone number changes recently
        update_phone_memberships()
        logging.info("Successfully finished a user poll to update availability and phone memberships")
        return True
    except Exception as e:
        logging.error(f"Failed when updating Close User availabilities because {str(e)}")
        return str(e)

## Method to map phone numbers to user ids for each of the Twilio numbers passed through in environmental arguments.
## For this to work, the Twilio number must be added as an Virtual group number in Close. We use Virtual group numbers as opposed to
## external Caller IDs because you can't update memberships on external Caller IDs.
def update_phone_memberships():
    try:
        user_ids_to_phones = {}
        phone_numbers = []

        for number in twilio_phone_numbers:
            phones = api.get('phone_number', params={ 'number': number })
            if phones['data']:
                phone = phones['data'][0]
                for participant in phone['participants']:
                    if participant.startswith('user_'):
                        if participant in user_ids_to_phones:
                            user_ids_to_phones[participant].append(number)
                        else:
                            user_ids_to_phones[participant] = [number]

        for worker_id in twilio_worker_ids_to_close_user_ids:
            user_id = twilio_worker_ids_to_close_user_ids[worker_id]
            if user_ids_to_phones.get(user_id):
                if len(close_user_ids_to_twilio_phone_numbers.get(user_id, [])) != len(user_ids_to_phones[user_id]):
                    update_worker_phone_numbers_attribute(worker_id, user_ids_to_phones[user_id])
            elif close_user_ids_to_twilio_phone_numbers.get(user_id, []) != []:
                update_worker_phone_numbers_attribute(worker_id, [])
        return True
    except Exception as e:
        logging.error(f"Failed when updating phone memberships because {str(e)}")
        return str(e)

## Method to either create or delete Twilio worker based on an updated membership status in Close.
def update_close_membership(user_id, action):
    try:
        if action == 'deactivated':
            worker_sid = close_user_ids_to_twilio_worker_ids.get(user_id)
            if worker_sid:
                del close_user_ids_to_twilio_worker_ids[user_id]
                if close_user_ids_to_current_calls.get(user_id):
                    del close_user_ids_to_current_calls[user_id]
                if twilio_worker_ids_to_close_user_ids.get(worker_sid):
                    del twilio_worker_ids_to_close_user_ids[worker_sid]
                remove_twilio_worker_by_worker_sid(worker_sid)
        if action == 'activated':
            user = api.get(f'user/{user_id}', params={ '_fields': 'first_name,last_name'})
            user_name = f"{user['first_name']} {user['last_name']}"
            if user_name and user_name != "":
                create_twilio_worker(user_id, user_name)
        return user_id
    except Exception as e:
        logging.error(f"Failed when a membership was {action} for {user_id} because {str(e)}")
        return str(e)

## Method to update Close group number participants based on current ongoing calls. This method is responsible for updating the members of
## Close group numbers that can receive certain calls, based on the participants of the Caller ID virtual numbers that correspond to the numbers in
## Twilio. In essence, this method syncs the Virtual Number Caller IDs, with the actual users that can receive calls in Close, based on if they have
## any ongoing calls.
def update_close_group_number_participation():
    try:
        for number in twilio_phone_numbers:
            if number in twilio_to_close_phone_mapping:
                phones = api.get('phone_number', params={ 'number': number })
                if phones['data']:
                    phone = phones['data'][0]
                    participants = phone['participants']
                    for user in list(close_user_ids_to_current_calls.keys()):
                        if user in participants:
                            participants.remove(user)
                    close_number = api.get('phone_number', params={ 'number': twilio_to_close_phone_mapping[number] })
                    if close_number['data'] and sorted(close_number['data'][0]['participants']) != sorted(participants):
                        api.put('phone_number/' + close_number['data'][0]['id'], data={ 'participants': participants })
        return True
    except Exception as e:
        logging.error(f"Failed to update Close group number participants because {str(e)}")
        return str(e)

## Method to process Close call webhooks in the integration. In this case, process means that when an "activity.call.updated"
## webhook comes in and the status of the call is "in-progress", we mark the Twilio worker corresponding to the Close user of the call as
## "Unavailable". Additionally, when a "completed" call webhook comes in, if the Close user currently has no other active calls,
## we mark them as available again.
def process_close_call_event(user_id, action, object_id):
    try:
        worker_id = close_user_ids_to_twilio_worker_ids.get(user_id)
        updated_stat = None

        ## If action is equal to updated, meaning a call was moved to in-progress, make sure the Close user moves to Unavailable in Twilio
        if action == 'updated':
            if user_id in close_user_ids_to_current_calls:
                close_user_ids_to_current_calls[user_id].append(object_id)
            else:
                close_user_ids_to_current_calls[user_id] = [object_id]
                updated_stat = 'Unavailable'

        ## If action is equal to completed, meaning a call was just finished, mark the Close user as "Available" in Twilio assuming that they have no other
        ## ongoing calls.
        elif action == 'completed':
            if close_user_ids_to_current_calls.get(user_id) and object_id in close_user_ids_to_current_calls[user_id]:
                close_user_ids_to_current_calls[user_id].remove(object_id)
                if len(close_user_ids_to_current_calls[user_id]) == 0:
                    del close_user_ids_to_current_calls[user_id]
                    updated_stat = 'Available'

        if worker_id and updated_stat and twilio_workers_availability_status.get(worker_id) != updated_stat:
            print(f"Updated status of {user_id} to {updated_stat}")
            update_close_group_number_participation()
            update_twilio_worker_status(worker_id, updated_stat)
        return True

    except Exception as e:
        logging.error(f"Failed when processing a Close call event because {str(e)}")
        return str(e)

## Method to initialize all possible variables when the integration first begins
def initialize_integration_variables():
    try:
        get_twilio_worker_statuses()
        get_twilio_workers()
        make_sure_all_memberships_have_workers_on_startup()
        get_twilio_taskrouter_queues()
        update_close_availability()
        update_close_group_number_participation()
        logging.info("Integration successfully initialized")
    except Exception as e:
        logging.error(f"Failed to initialize integration because {str(e)}")
        return str(e)

## Call initialize integration variables
initialize_integration_variables()
