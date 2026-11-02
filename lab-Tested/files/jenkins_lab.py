from flask import Blueprint, jsonify, render_template

import files.ansible as ansible

jenkins_bp = Blueprint("jenkins_lab", __name__)


def jenkins_creation_script(ctx):
    header = (
        f"NETWORK={ctx.network}\n"
        f"NAME={ctx.name('jenkins-server')}\n"
        f"PORT={ctx.web_port}\n"
    )

    return header + r"""set -Eeuo pipefail

command -v docker >/dev/null 2>&1 || {
    echo "Docker is not installed on the remote VM." >&2
    exit 127
}

IMAGE=jenkins/jenkins:lts

docker network inspect "$NETWORK" >/dev/null 2>&1 || \
    docker network create "$NETWORK" >/dev/null

# Pull only when the image is not already on the VM.
docker image inspect "$IMAGE" >/dev/null 2>&1 || \
    docker pull "$IMAGE" >/dev/null

# Reuse the container if it exists, otherwise create it.
# The web UI is published on a per-user host port (base port + user id).
if ! docker container inspect "$NAME" >/dev/null 2>&1; then
    docker run -d \
        --name "$NAME" \
        --hostname jenkins-server \
        --network "$NETWORK" \
        --network-alias jenkins-server \
        --restart unless-stopped \
        -p "$PORT":8080 \
        "$IMAGE" >/dev/null
else
    docker network connect "$NETWORK" "$NAME" >/dev/null 2>&1 || true
fi

docker start "$NAME" >/dev/null

running=$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || true)
[ "$running" = true ] || {
    echo "Jenkins container did not start." >&2
    exit 1
}

echo "Jenkins container is running."
"""


ansible.register_lab("jenkins", jenkins_creation_script)


@jenkins_bp.route("/jenkins")
def jenkins_page():
    return ansible.render_lab_template("jenkins.html", "jenkins")


@jenkins_bp.route("/jenkins/document")
def jenkins_document_page():
    return render_template(
        "document.html",
        host_ip=ansible.VM_HOST_IP,
        labs=ansible.LABS,
        LABS=ansible.LABS,
        networks=ansible.NETWORKS,
        current_lab="jenkins",
        slug="jenkins",
    )


@jenkins_bp.get("/api/jenkins/status")
def jenkins_status():
    try:
        return jsonify(ansible.lab_status("jenkins"))
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@jenkins_bp.post("/api/jenkins/create")
def jenkins_create():
    return ansible.start_lab_creation("jenkins")


@jenkins_bp.post("/api/jenkins/stop")
def jenkins_stop():
    return ansible.stop_lab("jenkins")


@jenkins_bp.post("/api/jenkins/destroy")
def jenkins_destroy():
    return ansible.destroy_lab_route_for("jenkins")


@jenkins_bp.post("/api/jenkins/containers/<container>/start")
def jenkins_container_start(container):
    return ansible.container_action("jenkins", container, "start")


@jenkins_bp.post("/api/jenkins/containers/<container>/stop")
def jenkins_container_stop(container):
    return ansible.container_action("jenkins", container, "stop")


@jenkins_bp.delete("/api/jenkins/containers/<container>")
def jenkins_container_delete(container):
    return ansible.container_action("jenkins", container, "delete")


@jenkins_bp.post("/api/jenkins/terminal/open")
def jenkins_terminal_open():
    return ansible.terminal_open_for_lab("jenkins")


ansible.app.register_blueprint(jenkins_bp)