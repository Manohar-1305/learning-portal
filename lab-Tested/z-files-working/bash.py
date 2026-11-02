from flask import Blueprint, jsonify, render_template

import files.ansible as ansible

bash_bp = Blueprint("bash_lab", __name__)


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
