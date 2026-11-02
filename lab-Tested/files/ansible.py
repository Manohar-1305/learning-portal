import json
import logging
import os
import shlex
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import paramiko
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session
from flask_socketio import SocketIO, join_room

BASE_DIR = Path(__file__).resolve().parent.parent
DOCUMENTS_DIR = BASE_DIR / "documents"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

load_dotenv(BASE_DIR / ".env")

VM_HOST_IP = os.getenv("VM_HOST_IP")
VM_HOST_USERNAME = os.getenv("VM_HOST_USERNAME")
VM_HOST_PASSWORD = os.getenv("VM_HOST_PASSWORD")
SSH_PORT = int(os.getenv("VM_HOST_PORT", "22"))
LAB_DURATION_SECONDS = int(os.getenv("LAB_DURATION_SECONDS", "3600"))
JENKINS_BASE_PORT = int(os.getenv("JENKINS_BASE_PORT", "18000"))

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

app = Flask(
    __name__,
    template_folder=str(TEMPLATES_DIR),
    static_folder=str(STATIC_DIR),
)
# No hardcoded fallback: if SECRET_KEY is not set, files/auth.py generates a
# random key and stores it in .secret_key.
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY") or None

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


# --- Per-user isolation ----------------------------------------------------
# Every user gets their own containers, Docker network, lab state, progress,
# terminal sessions and socket room. Containers are named <base>-u<user_id>.


def current_uid():
    return int(session["user_id"])


def user_room(uid):
    return f"user-{uid}"


class LabContext:
    def __init__(self, slug, uid):
        self.slug = slug
        self.uid = int(uid)
        self.suffix = f"-u{self.uid}"
        self.network = LABS[slug]["network"] + self.suffix

    def name(self, base):
        return base + self.suffix

    def base_name(self, real):
        if real.endswith(self.suffix):
            return real[: -len(self.suffix)]
        return real

    @property
    def names(self):
        return [self.name(base) for base in LABS[self.slug]["hosts"]]

    @property
    def web_port(self):
        return JENKINS_BASE_PORT + self.uid


def new_state():
    return {
        "creating": False,
        "setup_running": False,
        "created_at": None,
        "expires_at": None,
        "last_error": None,
    }


lab_state = {}


def get_state(uid, slug):
    with lab_lock:
        return lab_state.setdefault((uid, slug), new_state())


terminal_sessions = {}
socket_users = {}


@socketio.on("connect")
def socket_connect(*args):
    uid = session.get("user_id")

    if uid:
        join_room(user_room(int(uid)))
        socket_users[request.sid] = int(uid)


@socketio.on("disconnect")
def socket_disconnect(*args):
    socket_users.pop(request.sid, None)


def emit_to_user(uid, event, payload, sid=None):
    target = sid if sid and socket_users.get(sid) == uid else user_room(uid)

    try:
        socketio.emit(event, payload, to=target)
    except Exception:
        pass


PROTECTED_PAGES = ("/ansible", "/jenkins", "/bash")


@app.before_request
def require_login_for_labs():
    path = request.path
    is_api = path.startswith("/api/")
    is_page = any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in PROTECTED_PAGES
    )

    if not (is_api or is_page) or session.get("user_id"):
        return None

    if is_api:
        return jsonify({
            "success": False,
            "error": "Login required.",
        }), 401

    return redirect("/login?next=" + quote(path))


# -------------------------------------------------------------------------

# Each lab file registers its own creation script (and optional background
# post-setup) here. create_lab() only runs the handler of the lab triggered.
# Both functions receive a LabContext for the current user.
LAB_HANDLERS = {}


def register_lab(slug, script_fn, post_setup=None):
    LAB_HANDLERS[slug] = {
        "script": script_fn,
        "post_setup": post_setup,
    }


# --- Progress tracking ---------------------------------------------------
# Documents are ordered by file name (use 01-, 02-, ... prefixes). A document
# is "completed", "current" (the first incomplete one) or "locked".
# Optional automated test: documents/<same-name>.check.sh is executed inside
# the lab's controller container; exit code 0 completes the document.
# Without a .check.sh file the document is completed manually.
# Progress is stored per user: {"<user_id>": {"<lab>": [documents]}}.
PROGRESS_FILE = BASE_DIR / "progress.json"
DOCUMENT_SUFFIXES = {".md", ".txt", ".html"}
CHECK_CONTAINERS = {
    "ansible": "ansible-controller",
}

progress_lock = threading.RLock()


def load_progress_data():
    try:
        data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_progress_data(data):
    try:
        PROGRESS_FILE.write_text(
            json.dumps(data, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def get_completed(uid, slug):
    user_data = load_progress_data().get(str(uid), {})

    if not isinstance(user_data, dict):
        return []

    completed = user_data.get(slug, [])
    return completed if isinstance(completed, list) else []


def set_completed(uid, slug, done):
    data = load_progress_data()
    user_data = data.get(str(uid))

    if not isinstance(user_data, dict):
        user_data = {}

    user_data[slug] = done
    data[str(uid)] = user_data
    save_progress_data(data)


def document_names():
    names = []

    if DOCUMENTS_DIR.exists():
        for path in sorted(DOCUMENTS_DIR.rglob("*")):
            if (
                not path.is_file()
                or path.suffix.lower() not in DOCUMENT_SUFFIXES
            ):
                continue

            names.append(
                str(path.relative_to(DOCUMENTS_DIR)).replace("\\", "/")
            )

    return names


def document_check_path(name):
    return DOCUMENTS_DIR / Path(name).with_suffix(".check.sh")


def document_statuses(uid, slug):
    names = document_names()

    with progress_lock:
        completed = set(get_completed(uid, slug))

    statuses = {}
    current_found = False

    for name in names:
        if name in completed:
            statuses[name] = "completed"
        elif not current_found:
            statuses[name] = "current"
            current_found = True
        else:
            statuses[name] = "locked"

    return statuses


def progress_payload(uid, slug):
    statuses = document_statuses(uid, slug)
    names = document_names()
    total = len(names)
    done = sum(1 for name in names if statuses[name] == "completed")
    current = next(
        (name for name in names if statuses[name] == "current"),
        None,
    )

    documents = []

    for name in names:
        documents.append({
            "name": name,
            "title": Path(name).stem.replace("-", " ").replace(
                "_", " "
            ).title(),
            "status": statuses[name],
            "has_check": document_check_path(name).is_file(),
        })

    return {
        "success": True,
        "total": total,
        "completed": done,
        "percent": int(round((done / total) * 100)) if total else 0,
        "current": current,
        "complete": bool(total and done == total),
        "documents": documents,
    }


def reset_progress(uid, slug):
    with progress_lock:
        set_completed(uid, slug, [])


# -------------------------------------------------------------------------


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


def ansible_header(ctx):
    return (
        f"NETWORK={shlex.quote(ctx.network)}\n"
        f"CONTROLLER={shlex.quote(ctx.name('ansible-controller'))}\n"
        f"CLIENT={shlex.quote(ctx.name('ansible-client'))}\n"
    )


def ensure_ansible_setup(ctx):
    # Containers are named per user, but each user's private network gives
    # them the aliases "ansible-controller" and "ansible-client", so the
    # inventory below works unchanged.
    script = ansible_header(ctx) + r"""set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive

docker network inspect "$NETWORK" >/dev/null 2>&1 || \
    docker network create "$NETWORK" >/dev/null

# Reuse existing containers. Never remove and recreate them during setup.
if ! docker container inspect "$CONTROLLER" >/dev/null 2>&1; then
    docker run -d \
        --name "$CONTROLLER" \
        --hostname ansible-controller \
        --network "$NETWORK" \
        --network-alias ansible-controller \
        --restart unless-stopped \
        ubuntu:24.04 sleep infinity >/dev/null
fi

if ! docker container inspect "$CLIENT" >/dev/null 2>&1; then
    docker run -d \
        --name "$CLIENT" \
        --hostname ansible-client \
        --network "$NETWORK" \
        --network-alias ansible-client \
        --restart unless-stopped \
        ubuntu:24.04 sleep infinity >/dev/null
fi

docker start "$CONTROLLER" "$CLIENT" >/dev/null

# Install controller and client packages in parallel to cut setup time.
docker exec "$CONTROLLER" bash -lc '
    set -Eeuo pipefail
    export DEBIAN_FRONTEND=noninteractive

    if ! command -v ansible >/dev/null 2>&1 ||
       ! command -v sshpass >/dev/null 2>&1 ||
       ! command -v ssh >/dev/null 2>&1; then
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends ansible openssh-client sshpass python3
    fi
' &
controller_pid=$!

docker exec "$CLIENT" bash -lc '
    set -Eeuo pipefail
    export DEBIAN_FRONTEND=noninteractive

    if ! command -v sshd >/dev/null 2>&1 ||
       ! command -v python3 >/dev/null 2>&1; then
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends openssh-server python3
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
' &
client_pid=$!

wait "$controller_pid"
wait "$client_pid"

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
    return run_remote_script(script, timeout=900)


def ansible_creation_script(ctx):
    return ansible_header(ctx) + r"""set -Eeuo pipefail

command -v docker >/dev/null 2>&1 || {
    echo "Docker is not installed on the remote VM." >&2
    exit 127
}

docker network inspect "$NETWORK" >/dev/null 2>&1 || \
    docker network create "$NETWORK" >/dev/null

# Keep existing containers and their installed packages.
# Only create a container if it does not exist.
if ! docker container inspect "$CONTROLLER" >/dev/null 2>&1; then
    docker run -d \
        --name "$CONTROLLER" \
        --hostname ansible-controller \
        --network "$NETWORK" \
        --network-alias ansible-controller \
        --restart unless-stopped \
        ubuntu:24.04 sleep infinity >/dev/null
else
    docker network connect "$NETWORK" "$CONTROLLER" \
        >/dev/null 2>&1 || true
fi

if ! docker container inspect "$CLIENT" >/dev/null 2>&1; then
    docker run -d \
        --name "$CLIENT" \
        --hostname ansible-client \
        --network "$NETWORK" \
        --network-alias ansible-client \
        --restart unless-stopped \
        ubuntu:24.04 sleep infinity >/dev/null
else
    docker network connect "$NETWORK" "$CLIENT" \
        >/dev/null 2>&1 || true
fi

docker start "$CONTROLLER" "$CLIENT" >/dev/null

echo "Ansible containers are running."
"""


def shell_script_for_lab(slug, ctx):
    handler_entry = LAB_HANDLERS.get(slug)

    if handler_entry is None:
        raise RuntimeError(f"No creation handler registered for lab '{slug}'.")

    return handler_entry["script"](ctx)


def lab_status(slug):
    uid = current_uid()
    ctx = LabContext(slug, uid)
    bases = LABS[slug]["hosts"]
    quoted_names = " ".join(shlex.quote(name) for name in ctx.names)

    result = remote_exec(
        f"sudo -n docker inspect {quoted_names} 2>/dev/null",
        timeout=30,
    )

    containers = {}

    if result["success"] and result["stdout"].strip():
        try:
            for item in json.loads(result["stdout"]):
                state = item.get("State", {})
                real_name = item.get("Name", "").lstrip("/")
                containers[ctx.base_name(real_name)] = {
                    "state": state.get("Status", "unknown"),
                    "status": state.get("Status", "unknown"),
                    "running": bool(state.get("Running", False)),
                }
        except (json.JSONDecodeError, TypeError):
            pass

    for name in bases:
        containers.setdefault(name, {
            "state": "missing",
            "status": "Not created",
            "running": False,
        })

    with lab_lock:
        state = dict(get_state(uid, slug))

    remaining = None
    if state["expires_at"] is not None:
        remaining = max(0, int(state["expires_at"] - time.time()))

    all_running = all(containers[name]["running"] for name in bases)

    return {
        "success": True,
        "lab": slug,
        "containers": containers,
        "running": all_running,
        "has_containers": any(
            containers[name]["state"] != "missing" for name in bases
        ),
        "creating": state["creating"],
        "setting_up": state["setup_running"],
        "ready": bool(
            all_running
            and not state["creating"]
            and not state["setup_running"]
        ),
        "remaining_seconds": remaining,
        "last_error": state["last_error"],
        "web_port": ctx.web_port if slug == "jenkins" else None,
    }


def run_post_setup(slug, uid, post_setup):
    # Slow package/config work runs here, after the lab was already reported
    # as created. Silent: no extra logs, errors only go to last_error.
    success = True
    message = f"{slug.capitalize()} lab is ready."
    state = get_state(uid, slug)

    try:
        result = post_setup(LabContext(slug, uid))

        if result and not result.get("success", True):
            success = False
            message = (
                result.get("stderr", "").strip()
                or result.get("stdout", "").strip()
                or f"{slug.capitalize()} post-setup failed."
            )

            with lab_lock:
                state["last_error"] = message
    except Exception as exc:
        success = False
        message = str(exc)

        with lab_lock:
            state["last_error"] = message
    finally:
        with lab_lock:
            state["setup_running"] = False

    emit_to_user(
        uid,
        "lab_ready",
        {"lab": slug, "success": success, "message": message},
    )


def create_lab(slug, uid, sid=None, already_marked=False):
    state = get_state(uid, slug)
    ctx = LabContext(slug, uid)

    with lab_lock:
        if state["creating"] and not already_marked:
            return {
                "success": False,
                "error": "Lab creation is already in progress.",
            }

        state["creating"] = True
        state["last_error"] = None

    try:
        result = run_remote_script(
            shell_script_for_lab(slug, ctx),
            timeout=1800,
        )

        if not result["success"]:
            message = (
                result["stderr"].strip()
                or result["stdout"].strip()
                or "Lab creation failed."
            )

            with lab_lock:
                state["last_error"] = message

            emit_to_user(
                uid,
                "cluster_complete",
                {"lab": slug, "success": False, "message": message},
                sid,
            )

            return {"success": False, "error": message}

        names = " ".join(shlex.quote(name) for name in ctx.names)

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
                state["last_error"] = message

            emit_to_user(
                uid,
                "cluster_complete",
                {"lab": slug, "success": False, "message": message},
                sid,
            )

            return {"success": False, "error": message}

        now = time.time()

        post_setup = LAB_HANDLERS[slug].get("post_setup")

        with lab_lock:
            state["created_at"] = now
            state["expires_at"] = now + LAB_DURATION_SECONDS
            state["last_error"] = None

            # Mark setup as running before reporting completion so the UI
            # never sees a "running but not ready" gap.
            if post_setup is not None:
                state["setup_running"] = True

        logger.info(
            "LAB_CREATED user=%s lab=%s containers=%s",
            uid,
            slug,
            ",".join(ctx.names),
        )

        message = f"{slug.capitalize()} lab created successfully."

        emit_to_user(
            uid,
            "cluster_complete",
            {"lab": slug, "success": True, "message": message},
            sid,
        )

        if post_setup is not None:
            threading.Thread(
                target=run_post_setup,
                args=(slug, uid, post_setup),
                daemon=True,
            ).start()

        return {"success": True, "message": message}

    except Exception as exc:
        with lab_lock:
            state["last_error"] = str(exc)

        emit_to_user(
            uid,
            "cluster_complete",
            {"lab": slug, "success": False, "message": str(exc)},
            sid,
        )

        return {"success": False, "error": str(exc)}

    finally:
        with lab_lock:
            state["creating"] = False


def destroy_lab(slug, uid, expired=False):
    ctx = LabContext(slug, uid)
    hosts = " ".join(shlex.quote(name) for name in ctx.names)

    result = remote_exec(
        f"sudo -n docker rm -f {hosts} >/dev/null 2>&1 || true; "
        f"sudo -n docker network rm {shlex.quote(ctx.network)} "
        ">/dev/null 2>&1 || true; exit 0",
        timeout=120,
    )

    logger.info(
        "LAB_DESTROYED user=%s lab=%s containers=%s",
        uid,
        slug,
        ",".join(ctx.names),
    )

    state = get_state(uid, slug)

    with lab_lock:
        state["created_at"] = None
        state["expires_at"] = None
        state["last_error"] = None
        state["setup_running"] = False

    # Lab data is gone after destroy/expiry, so progress starts over.
    reset_progress(uid, slug)

    with terminal_lock:
        stale = [
            key for key, value in terminal_sessions.items()
            if value["lab"] == slug and value["uid"] == uid
        ]

        for key in stale:
            try:
                terminal_sessions[key]["channel"].close()
                terminal_sessions[key]["client"].close()
            except Exception:
                pass
            terminal_sessions.pop(key, None)

    return result


def expire_lab(slug, uid):
    try:
        destroy_lab(slug, uid, expired=True)
        emit_to_user(
            uid,
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

        with lab_lock:
            items = [(key, dict(value)) for key, value in lab_state.items()]

        for (uid, slug), state in items:
            expiry = state["expires_at"]

            if expiry is not None and expiry <= now and not state["creating"]:
                threading.Thread(
                    target=expire_lab,
                    args=(slug, uid),
                    daemon=True,
                ).start()


threading.Thread(target=expiry_worker, daemon=True).start()


def render_lab_template(template_name, slug):
    lab = LABS[slug]
    uid = current_uid()

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
        network=LabContext(slug, uid).network,
        jenkins_port=LabContext("jenkins", uid).web_port,
    )


def start_lab_creation(slug):
    payload = request.get_json(silent=True) or {}
    sid = payload.get("sid")
    uid = current_uid()
    state = get_state(uid, slug)

    with lab_lock:
        if state["creating"]:
            return jsonify({
                "success": False,
                "error": "Lab creation is already in progress.",
            }), 409

        state["creating"] = True

    threading.Thread(
        target=create_lab,
        args=(slug, uid, sid, True),
        daemon=True,
    ).start()

    return jsonify({
        "success": True,
        "message": f"{slug.capitalize()} lab creation started.",
    }), 202


def stop_lab(slug):
    uid = current_uid()
    ctx = LabContext(slug, uid)
    names = " ".join(shlex.quote(name) for name in ctx.names)

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

    state = get_state(uid, slug)

    with lab_lock:
        state["created_at"] = None
        state["expires_at"] = None

    return jsonify({
        "success": True,
        "message": f"{slug.capitalize()} lab stopped.",
    })


def destroy_lab_route_for(slug):
    result = destroy_lab(slug, current_uid())

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

    uid = current_uid()
    real_name = LabContext(slug, uid).name(container)

    if action == "delete":
        command = f"sudo -n docker rm -f {shlex.quote(real_name)}"
    else:
        command = f"sudo -n docker {action} {shlex.quote(real_name)}"

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
            "CONTAINER_DELETED user=%s lab=%s container=%s",
            uid,
            slug,
            real_name,
        )

    message = (
        f"{container} destroyed."
        if action == "delete"
        else f"{container} {action}ed."
    )

    return jsonify({"success": True, "message": message})


def terminal_open_for_lab(slug):
    payload = request.get_json(silent=True) or {}
    container = payload.get("container", "")

    if container not in LABS[slug]["hosts"]:
        return jsonify({
            "success": False,
            "error": "Container does not belong to this lab.",
        }), 400

    uid = current_uid()
    real_name = LabContext(slug, uid).name(container)
    state = get_state(uid, slug)

    with lab_lock:
        busy = state["creating"] or state["setup_running"]

    if busy:
        return jsonify({
            "success": False,
            "error": "Lab is still being set up. Please wait.",
        }), 409

    check = remote_exec(
        "sudo -n docker inspect -f '{{.State.Running}}' "
        + shlex.quote(real_name)
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
            + shlex.quote(real_name)
            + " bash"
        )
    except Exception:
        client.close()
        raise

    session_id = str(uuid.uuid4())

    with terminal_lock:
        terminal_sessions[session_id] = {
            "uid": uid,
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
    uid = current_uid()

    with terminal_lock:
        term = terminal_sessions.get(session_id)

    if term and term["uid"] == uid:
        return term

    return None


# Ansible lab: creation script + background setup registered here.
register_lab(
    "ansible",
    ansible_creation_script,
    post_setup=ensure_ansible_setup,
)


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
    setup = ensure_ansible_setup(LabContext("ansible", current_uid()))

    if not setup["success"]:
        return jsonify(setup), 500

    return jsonify(setup), 200


@app.post("/api/ansible/terminal/open")
def ansible_terminal_open():
    return terminal_open_for_lab("ansible")


# --- Progress tracking routes ---------------------------------------------


@app.get("/api/ansible/progress")
def ansible_progress():
    try:
        return jsonify(progress_payload(current_uid(), "ansible"))
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.post("/api/ansible/progress/complete")
def ansible_progress_complete():
    slug = "ansible"
    uid = current_uid()
    payload = request.get_json(silent=True) or {}
    name = payload.get("name", "")

    statuses = document_statuses(uid, slug)

    if name not in statuses:
        return jsonify({
            "success": False,
            "error": "Unknown document.",
        }), 404

    if statuses[name] == "locked":
        return jsonify({
            "success": False,
            "error": "Complete the previous document first.",
        }), 409

    if statuses[name] == "completed":
        return jsonify(progress_payload(uid, slug))

    check_path = document_check_path(name)

    if check_path.is_file():
        container = LabContext(slug, uid).name(CHECK_CONTAINERS[slug])

        try:
            check_script = check_path.read_text(encoding="utf-8")
        except OSError as exc:
            return jsonify({"success": False, "error": str(exc)}), 500

        try:
            result = run_remote_script(
                "docker exec -i "
                + shlex.quote(container)
                + " bash -lc "
                + shlex.quote(check_script),
                timeout=120,
            )
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 500

        if not result["success"]:
            output = (
                result["stdout"].strip()
                or result["stderr"].strip()
                or "Check failed."
            )
            return jsonify({
                "success": False,
                "error": "Check failed: " + output[-600:],
            }), 422

    with progress_lock:
        done = list(get_completed(uid, slug))

        if name not in done:
            done.append(name)

        set_completed(uid, slug, done)

    return jsonify(progress_payload(uid, slug))


@app.post("/api/ansible/progress/reset")
def ansible_progress_reset():
    uid = current_uid()
    reset_progress(uid, "ansible")
    return jsonify(progress_payload(uid, "ansible"))


# -------------------------------------------------------------------------


@app.get("/api/terminal/<session_id>/read")
def terminal_read(session_id):
    term = get_terminal_session(session_id)

    if not term:
        return jsonify({
            "success": False,
            "closed": True,
            "output": "",
        }), 404

    channel = term["channel"]
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
    term = get_terminal_session(session_id)

    if not term:
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
        term["channel"].send(data)
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.post("/api/terminal/<session_id>/resize")
def terminal_resize(session_id):
    term = get_terminal_session(session_id)

    if not term:
        return jsonify({
            "success": False,
            "error": "Terminal session not found.",
        }), 404

    payload = request.get_json(silent=True) or {}

    try:
        cols = max(20, min(int(payload.get("cols", 120)), 500))
        rows = max(5, min(int(payload.get("rows", 30)), 200))
        term["channel"].resize_pty(width=cols, height=rows)
        return jsonify({"success": True})
    except (ValueError, TypeError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.delete("/api/terminal/<session_id>")
def terminal_close(session_id):
    term = get_terminal_session(session_id)

    if term:
        with terminal_lock:
            terminal_sessions.pop(session_id, None)

        try:
            term["channel"].close()
        except Exception:
            pass

        try:
            term["client"].close()
        except Exception:
            pass

    return jsonify({"success": True})


@app.get("/api/documents")
def get_documents():
    documents = []

    # Progress tracking: locked documents never send their real content.
    statuses = document_statuses(current_uid(), "ansible")

    if DOCUMENTS_DIR.exists():
        for path in sorted(DOCUMENTS_DIR.rglob("*")):
            if (
                not path.is_file()
                or path.suffix.lower() not in {".md", ".txt", ".html"}
            ):
                continue

            name = str(
                path.relative_to(DOCUMENTS_DIR)
            ).replace("\\", "/")
            status = statuses.get(name, "current")
            title = path.stem.replace(
                "-", " "
            ).replace("_", " ").title()

            if status == "locked":
                if path.suffix.lower() == ".html":
                    content = (
                        "<h2>Locked</h2>"
                        "<p>Complete the previous document to unlock "
                        "this one.</p>"
                    )
                elif path.suffix.lower() == ".md":
                    content = (
                        "# Locked\n\n"
                        "Complete the previous document to unlock this one."
                    )
                else:
                    content = (
                        "Locked: complete the previous document to "
                        "unlock this one."
                    )

                documents.append({
                    "name": name,
                    "title": "🔒 " + title,
                    "content": content,
                    "locked": True,
                    "status": status,
                })
                continue

            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue

            documents.append({
                "name": name,
                "title": ("✓ " if status == "completed" else "") + title,
                "content": content,
                "locked": False,
                "status": status,
            })

    return jsonify({
        "success": True,
        "documents": documents,
    })