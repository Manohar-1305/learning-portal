from flask import Blueprint, jsonify, render_template

import files.ansible as ansible

jenkins_bp = Blueprint("jenkins_lab", __name__)


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