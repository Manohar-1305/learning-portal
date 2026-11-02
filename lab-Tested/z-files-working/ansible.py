
import json
import logging
import os
import shlex
import threading
import time
import uuid
from pathlib import Path

import paramiko
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DOCUMENTS_DIR = BASE_DIR / "documents"
TEMPLATES_DIR = BASE_DIR / "templates"

VM_HOST_IP = os.getenv("VM_HOST_IP")
VM_HOST_USERNAME = os.getenv("VM_HOST_USERNAME")
VM_HOST_PASSWORD = os.getenv("VM_HOST_PASSWORD")
SSH_PORT = int(os.getenv("VM_HOST_PORT", "22"))
LAB_DURATION_SECONDS = int(os.getenv("LAB_DURATION_SECONDS", "3600"))

NETWORKS = {
    "ansible": "ansible-lab-network",
    "jenkins": "jenkins-lab-network",
    "bash": "bash-lab-network",
}

LABS = {
    "ansible": {
        "hosts": ["ansible-controller", "ansible-client"],
        "network": NETWORKS["ansible"],
    },
    "jenkins": {
        "hosts": ["jenkins-server"],
        "network": NETWORKS["jenkins"],
    },
    "bash": {
        "hosts": ["bash-server"],
        "network": NETWORKS["bash"],
    },
}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv(
    "SECRET_KEY", "lab-portal-secret-key"
)

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
)

logging.basicConfig(level=logging.CRITICAL)

for noisy_logger in (
    "paramiko",
    "paramiko.transport",
    "werkzeug",
    "engineio",
    "socketio",
    "socketio.server",
):
    logging.getLogger(noisy_logger).setLevel(logging.CRITICAL)
    logging.getLogger(noisy_logger).propagate = False

logger = logging.getLogger("lab_dashboard")
logger.setLevel(logging.INFO)
logger.propagate = False

if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

lab_lock = threading.RLock()
terminal_lock = threading.RLock()

lab_state = {
    slug: {
        "creating": False,
        "created_at": None,
        "expires_at": None,
        "last_error": None,
    }
    for slug in LABS
}

terminal_sessions = {}


def ssh_connect():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=VM_HOST_IP,
        port=SSH_PORT,
        username=VM_HOST_USERNAME,
        password=VM_HOST_PASSWORD,
        timeout=15,
        banner_timeout=15,
        auth_timeout=15,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def remote_exec(command, timeout=300):
    client = ssh_connect()

    try:
        _, stdout, stderr = client.exec_command(
            command,
            timeout=timeout,
        )

        output = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()

        return {
            "success": exit_code == 0,
            "stdout": output,
            "stderr": error,
            "exit_code": exit_code,
        }
    finally:
        client.close()


def run_remote_script(script, timeout=1800):
    return remote_exec(
        "sudo -n bash -lc " + shlex.quote(script),
        timeout=timeout,
    )


def ansible_inventory_script():
    return r"""set -Eeuo pipefail

mkdir -p /etc/ansible

cat > /etc/ansible/hosts <<'EOF'
[clients]
ansible-client ansible_connection=ssh ansible_user=root ansible_password=labpassword
EOF

cat > /etc/ansible/ansible.cfg <<'EOF'
[defaults]
inventory = /etc/ansible/hosts
host_key_checking = False
interpreter_python = auto_silent
retry_files_enabled = False

[ssh_connection]
pipelining = True
EOF

chmod 644 /etc/ansible/hosts /etc/ansible/ansible.cfg

ANSIBLE_CONFIG=/etc/ansible/ansible.cfg \
    ansible-inventory -i /etc/ansible/hosts --list

ANSIBLE_CONFIG=/etc/ansible/ansible.cfg \
    ansible clients -i /etc/ansible/hosts -m ping
"""


def ensure_ansible_setup():
    script = r"""set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive

NETWORK=ansible-lab-network
CONTROLLER=ansible-controller
CLIENT=ansible-client

docker network inspect "$NETWORK" >/dev/null 2>&1 || \
    docker network create "$NETWORK" >/dev/null

# Reuse existing containers. Never remove and recreate them during setup.
if ! docker container inspect "$CONTROLLER" >/dev/null 2>&1; then
    docker run -d \
        --name "$CONTROLLER" \
        --hostname "$CONTROLLER" \
        --network "$NETWORK" \
        --restart unless-stopped \
        ubuntu:24.04 sleep infinity >/dev/null
fi

if ! docker container inspect "$CLIENT" >/dev/null 2>&1; then
    docker run -d \
        --name "$CLIENT" \
        --hostname "$CLIENT" \
        --network "$NETWORK" \
        --restart unless-stopped \
        ubuntu:24.04 sleep infinity >/dev/null
fi

docker start "$CONTROLLER" "$CLIENT" >/dev/null

# Install packages only if they are missing.
docker exec "$CONTROLLER" bash -lc '
    set -Eeuo pipefail
    export DEBIAN_FRONTEND=noninteractive

    if ! command -v ansible >/dev/null 2>&1 ||
       ! command -v sshpass >/dev/null 2>&1 ||
       ! command -v ssh >/dev/null 2>&1; then
        apt-get update -qq
        apt-get install -y -qq ansible openssh-client sshpass python3
    fi
'

# Install and configure SSH on the client only when needed.
docker exec "$CLIENT" bash -lc '
    set -Eeuo pipefail
    export DEBIAN_FRONTEND=noninteractive

    if ! command -v sshd >/dev/null 2>&1 ||
       ! command -v python3 >/dev/null 2>&1; then
        apt-get update -qq
        apt-get install -y -qq openssh-server python3
    fi

    mkdir -p /run/sshd
    ssh-keygen -A

    # Ensure cloud-init defaults do not override the required SSH settings.
    mkdir -p /etc/ssh/sshd_config.d
    cat > /etc/ssh/sshd_config.d/00-ansible-lab.conf <<EOF
PermitRootLogin yes
PasswordAuthentication yes
EOF

    if ! id root >/dev/null 2>&1; then
        echo "The root account is unavailable." >&2
        exit 1
    fi

    echo "root:labpassword" | chpasswd
    /usr/sbin/sshd -t
'

# Start sshd automatically whenever the client container starts.
docker exec "$CLIENT" bash -lc '
    set -Eeuo pipefail
    mkdir -p /run/sshd
    if ! pgrep -x sshd >/dev/null 2>&1; then
        /usr/sbin/sshd
    fi
'

# Ensure the controller has a persistent SSH key.
docker exec "$CONTROLLER" bash -lc '
    set -Eeuo pipefail
    mkdir -p /root/.ssh
    chmod 700 /root/.ssh

    if [ ! -f /root/.ssh/id_ed25519 ]; then
        ssh-keygen -q -t ed25519 -N "" -f /root/.ssh/id_ed25519
    fi
'

# Write the requested inventory and configuration automatically.
docker exec -i "$CONTROLLER" bash -lc \
    'mkdir -p /etc/ansible && cat > /etc/ansible/hosts' <<'EOF'
[clients]
ansible-client ansible_connection=ssh ansible_user=root ansible_password=labpassword
EOF

docker exec -i "$CONTROLLER" bash -lc \
    'cat > /etc/ansible/ansible.cfg' <<'EOF'
[defaults]
inventory = /etc/ansible/hosts
host_key_checking = False
interpreter_python = auto_silent
retry_files_enabled = False

[ssh_connection]
pipelining = True
EOF

docker exec "$CONTROLLER" chmod 644 \
    /etc/ansible/hosts /etc/ansible/ansible.cfg

# Wait briefly for SSH; do not wait for the previous 120-second maximum.
ssh_ready=false

for i in $(seq 1 15); do
    if docker exec "$CONTROLLER" bash -lc \
        'timeout 2 bash -c "</dev/tcp/ansible-client/22"' \
        >/dev/null 2>&1; then
        ssh_ready=true
        break
    fi
    sleep 1
done

if [ "$ssh_ready" != true ]; then
    docker exec "$CLIENT" bash -lc '
        mkdir -p /run/sshd
        /usr/sbin/sshd
    '
    sleep 1
fi

# Install the controller's public key on the client if passwordless SSH
# has not already been configured.
if ! docker exec "$CONTROLLER" bash -lc \
    'timeout 3 ssh -o BatchMode=yes -o ConnectTimeout=2 \
     -o StrictHostKeyChecking=no root@ansible-client true' \
     >/dev/null 2>&1; then

    docker exec "$CONTROLLER" bash -lc '
        set -Eeuo pipefail
        sshpass -p labpassword ssh-copy-id -i /root/.ssh/id_ed25519.pub \
            -o StrictHostKeyChecking=no \
            -o UserKnownHostsFile=/dev/null \
            -o ConnectTimeout=5 \
            -o PreferredAuthentications=password \
            -o PubkeyAuthentication=no \
            root@ansible-client
    '
fi

# Run the exact inventory and connectivity checks automatically.
docker exec "$CONTROLLER" bash -lc '
    set -Eeuo pipefail
    export ANSIBLE_CONFIG=/etc/ansible/ansible.cfg

    ansible-inventory -i /etc/ansible/hosts --list

    ansible clients -i /etc/ansible/hosts -m ping
'

echo "Ansible inventory, SSH, and ping configured successfully."
"""
    return run_remote_script(script, timeout=240)


def ansible_creation_script():
    return r"""set -Eeuo pipefail

command -v docker >/dev/null 2>&1 || {
    echo "Docker is not installed on the remote VM." >&2
    exit 127
}

NETWORK=ansible-lab-network

docker network inspect "$NETWORK" >/dev/null 2>&1 || \
    docker network create "$NETWORK" >/dev/null

# Keep existing containers and their installed packages.
# Only create a container if it does not exist.
if ! docker container inspect ansible-controller >/dev/null 2>&1; then
    docker run -d \
        --name ansible-controller \
        --hostname ansible-controller \
        --network "$NETWORK" \
        --restart unless-stopped \
        ubuntu:24.04 sleep infinity >/dev/null
else
    docker network connect "$NETWORK" ansible-controller \
        >/dev/null 2>&1 || true
fi

if ! docker container inspect ansible-client >/dev/null 2>&1; then
    docker run -d \
        --name ansible-client \
        --hostname ansible-client \
        --network "$NETWORK" \
        --restart unless-stopped \
        ubuntu:24.04 sleep infinity >/dev/null
else
    docker network connect "$NETWORK" ansible-client \
        >/dev/null 2>&1 || true
fi

docker start ansible-controller ansible-client >/dev/null

echo "Ansible containers are running."
"""


def shell_script_for_lab(slug):
    if slug == "ansible":
        return ansible_creation_script()

    network = LABS[slug]["network"]

    lines = [
        "set -Eeuo pipefail",
        "command -v docker >/dev/null 2>&1 || { echo 'Docker is not installed.' >&2; exit 127; }",
        f"NETWORK={shlex.quote(network)}",
        'docker network inspect "$NETWORK" >/dev/null 2>&1 || docker network create "$NETWORK" >/dev/null',
    ]

    if slug == "jenkins":
        lines.extend([
            "docker pull jenkins/jenkins:lts",
            "docker rm -f jenkins-server >/dev/null 2>&1 || true",
            'docker run -d --name jenkins-server --hostname jenkins-server --network "$NETWORK" --restart unless-stopped -p 8080:8080 -p 50000:50000 jenkins/jenkins:lts',
            "for i in $(seq 1 60); do running=$(docker inspect -f '{{.State.Running}}' jenkins-server 2>/dev/null || true); [ \"$running\" = true ] && break; sleep 2; done",
            "running=$(docker inspect -f '{{.State.Running}}' jenkins-server 2>/dev/null || true); [ \"$running\" = true ] || { echo 'Jenkins container did not start.' >&2; exit 1; }",
        ])

    elif slug == "bash":
        lines.extend([
            "docker pull ubuntu:24.04",
            "docker rm -f bash-server >/dev/null 2>&1 || true",
            'docker run -d --name bash-server --hostname bash-server --network "$NETWORK" --restart unless-stopped ubuntu:24.04 sleep infinity',
            "docker exec bash-server bash -lc 'apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y bash coreutils findutils grep sed gawk curl wget vim nano less procps iproute2 iputils-ping dnsutils net-tools tar gzip unzip jq'",
            "running=$(docker inspect -f '{{.State.Running}}' bash-server 2>/dev/null || true); [ \"$running\" = true ] || { echo 'Bash container did not start.' >&2; exit 1; }",
        ])

    return "\n".join(lines)


def lab_status(slug):
    names = LABS[slug]["hosts"]
    quoted_names = " ".join(shlex.quote(name) for name in names)

    result = remote_exec(
        f"sudo -n docker inspect {quoted_names} 2>/dev/null",
        timeout=30,
    )

    containers = {}

    if result["success"] and result["stdout"].strip():
        try:
            for item in json.loads(result["stdout"]):
                state = item.get("State", {})
                name = item.get("Name", "").lstrip("/")
                containers[name] = {
                    "state": state.get("Status", "unknown"),
                    "status": state.get("Status", "unknown"),
                    "running": bool(state.get("Running", False)),
                }
        except (json.JSONDecodeError, TypeError):
            pass

    for name in names:
        containers.setdefault(name, {
            "state": "missing",
            "status": "Not created",
            "running": False,
        })

    with lab_lock:
        state = dict(lab_state[slug])

    remaining = None
    if state["expires_at"] is not None:
        remaining = max(0, int(state["expires_at"] - time.time()))

    return {
        "success": True,
        "lab": slug,
        "containers": containers,
        "running": all(containers[name]["running"] for name in names),
        "has_containers": any(
            containers[name]["state"] != "missing" for name in names
        ),
        "creating": state["creating"],
        "remaining_seconds": remaining,
        "last_error": state["last_error"],
    }


def create_lab(slug, sid=None, already_marked=False):
    with lab_lock:
        if lab_state[slug]["creating"] and not already_marked:
            return {
                "success": False,
                "error": "Lab creation is already in progress.",
            }

        lab_state[slug]["creating"] = True
        lab_state[slug]["last_error"] = None

    try:
        result = run_remote_script(
            shell_script_for_lab(slug),
            timeout=1800,
        )

        if not result["success"]:
            message = (
                result["stderr"].strip()
                or result["stdout"].strip()
                or "Lab creation failed."
            )

            with lab_lock:
                lab_state[slug]["last_error"] = message

            if sid:
                socketio.emit(
                    "cluster_complete",
                    {"lab": slug, "success": False, "message": message},
                    to=sid,
                )

            return {"success": False, "error": message}

        if slug == "ansible":
            setup = ensure_ansible_setup()

            if not setup["success"]:
                message = (
                    setup["stderr"].strip()
                    or setup["stdout"].strip()
                    or "Ansible configuration failed."
                )

                with lab_lock:
                    lab_state[slug]["last_error"] = message

                if sid:
                    socketio.emit(
                        "cluster_complete",
                        {"lab": slug, "success": False, "message": message},
                        to=sid,
                    )

                return {"success": False, "error": message}

        names = " ".join(
            shlex.quote(name) for name in LABS[slug]["hosts"]
        )

        verify_command = (
            f"set -e; docker start {names} >/dev/null; "
            f"for name in {names}; do "
            "running=$(docker inspect -f '{{.State.Running}}' \"$name\"); "
            '[ "$running" = true ] || exit 1; done'
        )

        verify = remote_exec(
            "sudo -n bash -lc " + shlex.quote(verify_command),
            timeout=120,
        )

        if not verify["success"]:
            message = (
                verify["stderr"].strip()
                or verify["stdout"].strip()
                or "One or more containers failed to start."
            )

            with lab_lock:
                lab_state[slug]["last_error"] = message

            if sid:
                socketio.emit(
                    "cluster_complete",
                    {"lab": slug, "success": False, "message": message},
                    to=sid,
                )

            return {"success": False, "error": message}

        now = time.time()

        with lab_lock:
            lab_state[slug]["created_at"] = now
            lab_state[slug]["expires_at"] = now + LAB_DURATION_SECONDS
            lab_state[slug]["last_error"] = None

        logger.info(
            "LAB_CREATED lab=%s containers=%s",
            slug,
            ",".join(LABS[slug]["hosts"]),
        )

        message = f"{slug.capitalize()} lab created successfully."

        if sid:
            socketio.emit(
                "cluster_complete",
                {"lab": slug, "success": True, "message": message},
                to=sid,
            )

        return {"success": True, "message": message}

    except Exception as exc:
        with lab_lock:
            lab_state[slug]["last_error"] = str(exc)

        if sid:
            socketio.emit(
                "cluster_complete",
                {"lab": slug, "success": False, "message": str(exc)},
                to=sid,
            )

        return {"success": False, "error": str(exc)}

    finally:
        with lab_lock:
            lab_state[slug]["creating"] = False


def destroy_lab(slug, expired=False):
    network = LABS[slug]["network"]
    hosts = " ".join(
        shlex.quote(name) for name in LABS[slug]["hosts"]
    )

    result = remote_exec(
        f"sudo -n docker rm -f {hosts} >/dev/null 2>&1 || true; "
        f"sudo -n docker network rm {shlex.quote(network)} "
        ">/dev/null 2>&1 || true; exit 0",
        timeout=120,
    )

    logger.info(
        "LAB_DESTROYED lab=%s containers=%s",
        slug,
        ",".join(LABS[slug]["hosts"]),
    )

    with lab_lock:
        lab_state[slug]["created_at"] = None
        lab_state[slug]["expires_at"] = None
        lab_state[slug]["last_error"] = None

    with terminal_lock:
        stale = [
            key for key, value in terminal_sessions.items()
            if value["lab"] == slug
        ]

        for key in stale:
            try:
                terminal_sessions[key]["channel"].close()
                terminal_sessions[key]["client"].close()
            except Exception:
                pass
            terminal_sessions.pop(key, None)

    return result


def expire_lab(slug):
    try:
        destroy_lab(slug, expired=True)
        socketio.emit(
            "cluster_expired",
            {
                "lab": slug,
                "message": f"{slug.capitalize()} lab expired and was destroyed.",
            },
        )
    except Exception:
        pass


def expiry_worker():
    while True:
        time.sleep(5)
        now = time.time()

        for slug in LABS:
            with lab_lock:
                expiry = lab_state[slug]["expires_at"]
                creating = lab_state[slug]["creating"]

            if expiry is not None and expiry <= now and not creating:
                threading.Thread(
                    target=expire_lab,
                    args=(slug,),
                    daemon=True,
                ).start()


threading.Thread(target=expiry_worker, daemon=True).start()


def render_lab_template(template_name, slug):
    lab = LABS[slug]

    return render_template(
        template_name,
        host_ip=VM_HOST_IP,
        labs=LABS,
        LABS=LABS,
        networks=NETWORKS,
        current_lab=slug,
        slug=slug,
        lab=lab,
        hosts=lab["hosts"],
        network=lab["network"],
    )


def start_lab_creation(slug):
    payload = request.get_json(silent=True) or {}
    sid = payload.get("sid")

    with lab_lock:
        if lab_state[slug]["creating"]:
            return jsonify({
                "success": False,
                "error": "Lab creation is already in progress.",
            }), 409

        lab_state[slug]["creating"] = True

    threading.Thread(
        target=create_lab,
        args=(slug, sid, True),
        daemon=True,
    ).start()

    return jsonify({
        "success": True,
        "message": f"{slug.capitalize()} lab creation started.",
    }), 202


def stop_lab(slug):
    names = " ".join(
        shlex.quote(name) for name in LABS[slug]["hosts"]
    )

    result = remote_exec(
        f"sudo -n docker stop {names} >/dev/null",
        timeout=120,
    )

    if not result["success"]:
        return jsonify({
            "success": False,
            "error": result["stderr"].strip()
            or result["stdout"].strip()
            or "Unable to stop lab.",
        }), 500

    with lab_lock:
        lab_state[slug]["created_at"] = None
        lab_state[slug]["expires_at"] = None

    return jsonify({
        "success": True,
        "message": f"{slug.capitalize()} lab stopped.",
    })


def destroy_lab_route_for(slug):
    result = destroy_lab(slug)

    if not result["success"]:
        return jsonify({
            "success": False,
            "error": result["stderr"].strip()
            or "Unable to destroy lab.",
        }), 500

    return jsonify({
        "success": True,
        "message": f"{slug.capitalize()} lab destroyed.",
    })


def container_action(slug, container, action):
    if container not in LABS[slug]["hosts"]:
        return jsonify({
            "success": False,
            "error": "Unknown container.",
        }), 404

    if action == "delete":
        command = f"sudo -n docker rm -f {shlex.quote(container)}"
    else:
        command = f"sudo -n docker {action} {shlex.quote(container)}"

    result = remote_exec(command, timeout=60)

    if not result["success"]:
        return jsonify({
            "success": False,
            "error": result["stderr"].strip()
            or result["stdout"].strip()
            or f"Unable to {action} container.",
        }), 500

    if action == "delete":
        logger.info(
            "CONTAINER_DELETED lab=%s container=%s",
            slug,
            container,
        )

    message = (
        f"{container} destroyed."
        if action == "delete"
        else f"{container} {action}ed."
    )

    return jsonify({"success": True, "message": message})


@app.route("/")
def home():
    return render_template(
        "home.html",
        host_ip=VM_HOST_IP,
        labs=LABS,
        LABS=LABS,
        networks=NETWORKS,
    )


@app.route("/ansible")
def ansible_page():
    return render_lab_template("ansible.html", "ansible")


@app.route("/ansible/document")
def ansible_document_page():
    return render_template(
        "document.html",
        host_ip=VM_HOST_IP,
        labs=LABS,
        LABS=LABS,
        networks=NETWORKS,
        current_lab="ansible",
        slug="ansible",
    )


@app.get("/api/ansible/status")
def ansible_status():
    try:
        return jsonify(lab_status("ansible"))
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.post("/api/ansible/create")
def ansible_create():
    return start_lab_creation("ansible")


@app.post("/api/ansible/stop")
def ansible_stop():
    return stop_lab("ansible")


@app.post("/api/ansible/destroy")
def ansible_destroy():
    return destroy_lab_route_for("ansible")


@app.post("/api/ansible/containers/<container>/start")
def ansible_container_start(container):
    return container_action("ansible", container, "start")


@app.post("/api/ansible/containers/<container>/stop")
def ansible_container_stop(container):
    return container_action("ansible", container, "stop")


@app.delete("/api/ansible/containers/<container>")
def ansible_container_delete(container):
    return container_action("ansible", container, "delete")


@app.get("/api/health")
def health():
    result = remote_exec(
        "sudo -n docker version --format '{{.Server.Version}}'",
        timeout=20,
    )

    if not result["success"]:
        return jsonify({
            "success": False,
            "error": result["stderr"].strip()
            or "Cannot connect to Docker on the remote host.",
        }), 503

    return jsonify({
        "success": True,
        "docker_version": result["stdout"].strip(),
    })


@app.post("/api/ansible/ping")
def ansible_ping():
    setup = ensure_ansible_setup()

    if not setup["success"]:
        return jsonify(setup), 500

    return jsonify(setup), 200


@app.post("/api/ansible/terminal/open")
def ansible_terminal_open():
    return terminal_open_for_lab("ansible")


def terminal_open_for_lab(slug):
    payload = request.get_json(silent=True) or {}
    container = payload.get("container", "")

    if container not in LABS[slug]["hosts"]:
        return jsonify({
            "success": False,
            "error": "Container does not belong to this lab.",
        }), 400

    check = remote_exec(
        "sudo -n docker inspect -f '{{.State.Running}}' "
        + shlex.quote(container)
        + " 2>/dev/null",
        timeout=20,
    )

    if not check["success"] or check["stdout"].strip() != "true":
        return jsonify({
            "success": False,
            "error": f"{container} is not running. Create the lab first.",
        }), 409

    client = ssh_connect()

    try:
        channel = client.get_transport().open_session()
        channel.get_pty(term="xterm-256color", width=120, height=30)
        channel.exec_command(
            "sudo -n docker exec -it "
            + shlex.quote(container)
            + " bash"
        )
    except Exception:
        client.close()
        raise

    session_id = str(uuid.uuid4())

    with terminal_lock:
        terminal_sessions[session_id] = {
            "lab": slug,
            "container": container,
            "client": client,
            "channel": channel,
        }

    return jsonify({
        "success": True,
        "session_id": session_id,
    })


def get_terminal_session(session_id):
    with terminal_lock:
        return terminal_sessions.get(session_id)


@app.get("/api/terminal/<session_id>/read")
def terminal_read(session_id):
    session = get_terminal_session(session_id)

    if not session:
        return jsonify({
            "success": False,
            "closed": True,
            "output": "",
        }), 404

    channel = session["channel"]
    chunks = []

    try:
        while channel.recv_ready():
            chunks.append(
                channel.recv(65536).decode("utf-8", errors="replace")
            )

        while channel.recv_stderr_ready():
            chunks.append(
                channel.recv_stderr(65536).decode("utf-8", errors="replace")
            )

        return jsonify({
            "success": True,
            "closed": channel.exit_status_ready() or channel.closed,
            "output": "".join(chunks),
        })
    except Exception as exc:
        return jsonify({
            "success": False,
            "closed": True,
            "output": "",
            "error": str(exc),
        }), 500


@app.post("/api/terminal/<session_id>/write")
def terminal_write(session_id):
    session = get_terminal_session(session_id)

    if not session:
        return jsonify({
            "success": False,
            "error": "Terminal session not found.",
        }), 404

    payload = request.get_json(silent=True) or {}
    data = payload.get("data", "")

    if not isinstance(data, str):
        return jsonify({
            "success": False,
            "error": "Terminal input must be text.",
        }), 400

    try:
        session["channel"].send(data)
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.post("/api/terminal/<session_id>/resize")
def terminal_resize(session_id):
    session = get_terminal_session(session_id)

    if not session:
        return jsonify({
            "success": False,
            "error": "Terminal session not found.",
        }), 404

    payload = request.get_json(silent=True) or {}

    try:
        cols = max(20, min(int(payload.get("cols", 120)), 500))
        rows = max(5, min(int(payload.get("rows", 30)), 200))
        session["channel"].resize_pty(width=cols, height=rows)
        return jsonify({"success": True})
    except (ValueError, TypeError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.delete("/api/terminal/<session_id>")
def terminal_close(session_id):
    with terminal_lock:
        session = terminal_sessions.pop(session_id, None)

    if session:
        try:
            session["channel"].close()
        except Exception:
            pass

        try:
            session["client"].close()
        except Exception:
            pass

    return jsonify({"success": True})


@app.get("/api/documents")
def get_documents():
    documents = []

    if DOCUMENTS_DIR.exists():
        for path in sorted(DOCUMENTS_DIR.rglob("*")):
            if (
                not path.is_file()
                or path.suffix.lower() not in {".md", ".txt", ".html"}
            ):
                continue

            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue

            documents.append({
                "name": str(
                    path.relative_to(DOCUMENTS_DIR)
                ).replace("\\", "/"),
                "title": path.stem.replace(
                    "-", " "
                ).replace("_", " ").title(),
                "content": content,
            })

    return jsonify({
        "success": True,
        "documents": documents,
    })
