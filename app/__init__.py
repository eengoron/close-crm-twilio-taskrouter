from flask import Flask

app = Flask(__name__)
from app import routes  # noqa

if __name__ == '__main__':
    app.run(use_reloader=False, debug=True)
