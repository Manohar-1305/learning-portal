from flask import Blueprint, jsonify, render_template

import files.ansible as ansible

bash_bp = Blueprint("bash_lab", __name__)


def bash_creation_script(ctx):
    header = (
        f"NETWORK={ctx.network}\n"
        f"NAME={ctx.name('bash-server')}\n"
    )

    return header + r"""set -Eeuo pipefail

command -v docker >/dev/null 2>&1 || {
    echo "Docker is not installed on the remote VM." >&2
    exit 127
}

docker network inspect "$NETWORK" >/dev/null 2>&1 || \
    docker network create "$NETWORK" >/dev/null

# Pull only when the image is not already on the VM.
docker image inspect ubuntu:24.04 >/dev/null 2>&1 || \
    docker pull ubuntu:24.04 >/dev/null

# Reuse the container if it exists, otherwise create it.
if ! docker container inspect "$NAME" >/dev/null 2>&1; then
    docker run -d \
        --name "$NAME" \
        --hostname bash-server \
        --network "$NETWORK" \
        --network-alias bash-server \
        --restart unless-stopped \
        ubuntu:24.04 sleep infinity >/dev/null
else
    docker network connect "$NETWORK" "$NAME" >/dev/null 2>&1 || true
fi

docker start "$NAME" >/dev/null

running=$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || true)
[ "$running" = true ] || {
    echo "Bash container did not start." >&2
    exit 1
}

echo "Bash container is running."
"""


def bash_post_setup(ctx):
    # Runs in the background after the lab is reported as created.
    header = f"NAME={ctx.name('bash-server')}\n"

    script = header + r"""set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive

if ! docker exec "$NAME" bash -lc \
    'command -v jq >/dev/null 2>&1 && command -v vim >/dev/null 2>&1 && command -v ping >/dev/null 2>&1'; then
    docker exec "$NAME" bash -lc '
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y -qq bash coreutils findutils grep sed gawk curl wget vim nano less procps iproute2 iputils-ping dnsutils net-tools tar gzip unzip jq
    '
fi

echo "Bash lab tools installed."
"""
    return ansible.run_remote_script(script, timeout=900)


ansible.register_lab(
    "bash",
    bash_creation_script,
    post_setup=bash_post_setup,
)


@bash_bp.route("/bash")
def bash_page():
    return ansible.render_lab_template("bash.html", "bash")


@bash_bp.route("/bash/document")
def bash_document_page():
    return render_template(
        "document.html",
        host_ip=ansible.VM_HOST_IP,
        labs=ansible.LABS,
        LABS=ansible.LABS,
        networks=ansible.NETWORKS,
        current_lab="bash",
        slug="bash",
    )


@bash_bp.get("/api/bash/status")
def bash_status():
    try:
        return jsonify(ansible.lab_status("bash"))
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@bash_bp.post("/api/bash/create")
def bash_create():
    return ansible.start_lab_creation("bash")


@bash_bp.post("/api/bash/stop")
def bash_stop():
    return ansible.stop_lab("bash")


@bash_bp.post("/api/bash/destroy")
def bash_destroy():
    return ansible.destroy_lab_route_for("bash")


@bash_bp.post("/api/bash/containers/<container>/start")
def bash_container_start(container):
    return ansible.container_action("bash", container, "start")


@bash_bp.post("/api/bash/containers/<container>/stop")
def bash_container_stop(container):
    return ansible.container_action("bash", container, "stop")


@bash_bp.delete("/api/bash/containers/<container>")
def bash_container_delete(container):
    return ansible.container_action("bash", container, "delete")


@bash_bp.post("/api/bash/terminal/open")
def bash_terminal_open():
    return ansible.terminal_open_for_lab("bash")


ansible.app.register_blueprint(bash_bp)