import json
import logging

from flask import request

from app import app

from .methods import (
    delete_twilio_worker_from_close_user_id,
    dial_redirected_phone_number,
    process_close_group_update,
    redirect_key_press_to_vm,
    send_call_to_queue,
    send_redirect_instruction_on_assignment_callback,
    setup_wait_url,
    update_all_twilio_statuses_and_group_number_participants,
)

# Format logging
log_format = "[%(asctime)s] %(levelname)s %(message)s"
logging.basicConfig(level=logging.INFO, format=log_format)

#############
# Close Routes
#############

@app.route('/deactivate-membership/', methods=['POST'])
def delete_twilio_worker():
    """Delete a Twilio Worker when a Close membership is deactivated."""
    try:
        data = json.loads(request.data)
        event_data = data['event']
        if event_data.get('user_id'):
            delete_twilio_worker_from_close_user_id(
                event_data['user_id']
            )
            logging.info(
                f"Deleted {event_data['user_id']}'s Twilio Worker because they were made inactive.'"
            )
        return "Webhook processed successfully", 200
    except Exception as e:
        logging.error(f"Failed when trying to update a user because {str(e)}")
        return str(e), 400


@app.route('/close-completed-call/', methods=['POST'])
def close_completed_call():
    """
    Update the Close status of all users when there is a completed call webhook.
    We do this to keep the statuses of every user up to date.
    """
    try:
        update_all_twilio_statuses_and_group_number_participants()
        return "Webhook processed successfully", 200
    except Exception as e:
        logging.error(
            f"There was an error that happened when trying to update users when a call was created or completed because {str(e)}"
        )
        return str(e), 400


@app.route('/user-manager-group-updated/', methods=['POST'])
def updated_group():
    """
    Process group updates in Close, since they may affect who's
    available.
    """
    try:
        data = json.loads(request.data)
        event_data = data['event']
        if event_data.get('data') and 'members' in event_data.get(
            'changed_fields', []
        ):
            process_close_group_update(
                event_data['object_id'], event_data['data'].get('members', [])
            )
            logging.info(
                f"Successfully processed update for {event_data['object_id']}"
            )
        return "Success", 200
    except Exception as e:
        logging.error(f'Failed to process group update because {str(e)}')
        return str(e), 400


#############
# Twilio Routes
#############


@app.route('/incoming-call/', methods=['POST'])
def create_task():
    """
    Accept incoming calls in Twilio and add them to the appropriate
    Task Queue.
    """
    try:
        update_all_twilio_statuses_and_group_number_participants()
        if request.values.get('To'):
            return send_call_to_queue(request), 200
        return "Successfully sent a call to the queue", 200
    except Exception as e:
        logging.error(
            f"Failed when creating a task for a new call because {str(e)}"
        )
        return str(e), 400


@app.route('/assignment-callback/', methods=['POST'])
def assignment_callback():
    """
    Rings the correct Close group number based on the that the call goes
    into.
    """
    try:
        task_attributes = request.values.get('TaskAttributes')
        task_id = request.values.get('TaskSid')
        if task_attributes and task_id:
            task_attributes = json.loads(task_attributes)
            return (
                send_redirect_instruction_on_assignment_callback(
                    task_id, task_attributes
                ),
                200,
            )
    except Exception as e:
        logging.error(
            f"Failed when assigning an activity to a user because {str(e)}"
        )
        return str(e), 400


@app.route('/redirect-task/', methods=['POST'])
def redirect_task():
    """
    Redirect a task to a group number when there is an available user. We have
    to use a redirect as opposed to the instruction dequeue, because the
    instruction dequeue will not show us the original Caller ID when moving
    a call into Close.
    """
    try:
        return dial_redirected_phone_number(request), 200
    except Exception as e:
        logging.error(
            f"Failed when redirecting a queued activity to a phone number because {str(e)}"
        )
        return str(e), 400


@app.route('/wait-url/', methods=['POST'])
def wait_url():
    """
    Setup the wait-url to play the wait music and ask the user for an input
    at any time as well.
    """
    try:
        return setup_wait_url(), 200
    except Exception as e:
        logging.error(f"Failed when setting up the wait url because {str(e)}")
        return str(e), 400


@app.route('/forward-to-vm/', methods=['POST'])
def forward_to_vm():
    """
    Forward the caller into a voicemail box in Close, if they decide they want
    to leave the queue via pressing any key while waiting.
    """
    try:
        return redirect_key_press_to_vm(request)
    except Exception as e:
        logging.error(
            f"Failed when redirecting a queued task after a key press to escape because {str(e)}"
        )
        return str(e), 400
