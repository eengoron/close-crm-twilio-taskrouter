from app import app
import json
import logging
from flask import request
from .methods import update_close_membership, process_close_call_event, twiml, send_call_to_queue, send_redirect_instruction_on_assignment_callback, dial_redirected_phone_number, setup_wait_url, redirect_key_press_to_vm

## Format logging
log_format = "[%(asctime)s] %(levelname)s %(message)s"
logging.basicConfig(level=logging.INFO, format=log_format)

##########################
# Close Routes
##########################

## Route to update Twilio Workers when Close memberships are activated or deactivated
## If there is a valid user_id in the event data, this route uses the update_close_membership method to
## either create or delete a worker based on whether or not the user was activated or deactivated.
@app.route('/membership-updates/', methods=['POST'])
def index():
    try:
        data = json.loads(request.data)
        event_data = data['event']
        if event_data['data'].get('user_id'):
            update_close_membership(event_data['data']['user_id'], event_data['action'])
            logging.info(f"Updated membership of {event_data['data']['user_id']} because it was {event_data['action']}")
        return "Webhook processed successfully", 200
    except Exception as e:
        logging.error(f"Failed when trying to update a user because {str(e)}")
        return str(e), 400

## Route to update Close availability of a user when someone accepts an incoming call or makes an outgoing one
## This method uses updated and created webhooks to figure out when a new call goes out of or comes into Close. If a user in Close accepts a call
## they will be removed from Twilio group numbers in Close and they will be moved to "Unavailable" status in Twilio.
@app.route('/close-call-updates/', methods=['POST'])
def close_call_in_progress():
    try:
        event = json.loads(request.data)['event']
        if event.get('user_id'):
            process_close_call_event(event['user_id'], event['action'], event['object_id'])
        return "Webhook processed successfully", 200
    except Exception as e:
        logging.error(f"There was an error that happened when trying to update users when a call was created or completed because {str(e)}")
        return str(e), 400

##########################
# Twilio Routes
##########################

## Route to accept incoming calls from Twilio numbers and add them to the correct task queue.
@app.route('/incoming-call/', methods=['POST'])
def create_task():
    try:
        if request.values.get('To'):
            return send_call_to_queue(request), 200
        return "Successfully sent a call to the queue", 200
    except Exception as e:
        logging.error(f"Failed when creating a task for a new call because {str(e)}")
        return str(e), 400

## Route for assignment callbacks, which will ring the correct Close group number based on the that the call goes into.
@app.route('/assignment-callback/', methods=['POST'])
def assignment_callback():
    try:
        task_attributes = request.values.get('TaskAttributes')
        task_id = request.values.get('TaskSid')
        if task_attributes and task_id:
            task_attributes = json.loads(task_attributes)
            return send_redirect_instruction_on_assignment_callback(task_id, task_attributes), 200
    except Exception as e:
        logging.error(f"Failed when assigning an activity to a user because {str(e)}")
        return str(e), 400

## Route for redirecting the task to a group number when there is an available user. We have to use a redirect as opposed to
## the instruction dequeue, because the instruction dequeue will not show us the original Caller ID when moving a call into Close.
@app.route('/redirect-task/', methods=['POST'])
def redirect_task():
    try:
        return dial_redirected_phone_number(request), 200
    except Exception as e:
        logging.error(f"Failed when redirecting a queued activity to a phone number because {str(e)}")
        return str(e), 400

## Route to setup the wait-url to play the wait music and ask the user for an input at any time as well.
@app.route('/wait-url/', methods=['POST'])
def wait_url():
    try:
        return setup_wait_url(), 200
    except Exception as e:
        logging.error(f"Failed when setting up the wait url because {str(e)}")
        return str(e), 400

## Route to forward the caller into a voicemail box in Close, if they decide they want to leave the queue
## via pressing any key while waiting.
@app.route('/forward-to-vm/', methods=['POST'])
def forward_to_vm():
    try:
        return redirect_key_press_to_vm(request)
    except Exception as e:
        logging.error(f"Failed when redirecting a queued task after a key press to escape because {str(e)}")
        return str(e), 400
