from flask import Flask, session
from apscheduler.schedulers.background import BackgroundScheduler
import logging
import os
from .methods import update_close_availability

log = logging.getLogger('apscheduler.executors.default')
logging.getLogger('apscheduler.executors.default').propagate = False
log.setLevel(logging.WARNING)

fmt = logging.Formatter('%(levelname)s:%(name)s:%(message)s')
h = logging.StreamHandler()
h.setFormatter(fmt)
log.addHandler(h)

def job1():
    update_close_availability()

scheduler = BackgroundScheduler()
scheduler.add_job(job1,"interval", seconds=int(os.environ.get('seconds')))
scheduler.start()
app = Flask(__name__)

from app import routes
if __name__ == '__main__':
    app.run(use_reloader=False, debug=True)
