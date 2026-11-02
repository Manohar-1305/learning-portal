from dotenv import load_dotenv

load_dotenv()

import os
import files.ansible as ansible
import files.jenkins_lab as jenkins_lab
import files.bash as bash
import files.auth as auth
import files.users as users
import files.contribute as contribute
import files.admin as admin
import files.auth as auth



if __name__ == "__main__":
    ansible.socketio.run(
        ansible.app,
        host="0.0.0.0",
        port=5000,
        debug=False,
        allow_unsafe_werkzeug=True,
    )