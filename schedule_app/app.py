"""Employee work-schedule web app.

A small Flask application to:

  * manage employees (name, email, phone, role),
  * enter shifts for each employee,
  * send each employee their upcoming schedule by email and/or SMS.

Run it with:

    python3 app.py

then open http://127.0.0.1:5000 in a browser. Delivery settings (SMTP for
email, Twilio for SMS) are read from environment variables -- see
.env.example and README.md.
"""

from __future__ import annotations

import os

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

import database as db
import notifications as notify


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-change-me")

    db.init_db()

    # ------------------------------------------------------------------ #
    # Dashboard
    # ------------------------------------------------------------------ #
    @app.route("/")
    def index():
        employees = db.list_employees()
        upcoming = db.list_all_shifts(from_date=db.today_iso())
        return render_template(
            "index.html",
            employees=employees,
            upcoming=upcoming,
            email_ready=notify.email_configured(),
            sms_ready=notify.sms_configured(),
        )

    # ------------------------------------------------------------------ #
    # Employees
    # ------------------------------------------------------------------ #
    @app.route("/employees")
    def employees():
        return render_template("employees.html", employees=db.list_employees())

    @app.route("/employees/add", methods=["POST"])
    def add_employee():
        name = request.form.get("name", "").strip()
        if not name:
            flash("Employee name is required.", "error")
            return redirect(url_for("employees"))
        db.add_employee(
            name=name,
            email=request.form.get("email", ""),
            phone=request.form.get("phone", ""),
            role=request.form.get("role", ""),
        )
        flash(f"Added {name}.", "success")
        return redirect(url_for("employees"))

    @app.route("/employees/<int:emp_id>/edit", methods=["POST"])
    def edit_employee(emp_id: int):
        name = request.form.get("name", "").strip()
        if not name:
            flash("Employee name is required.", "error")
            return redirect(url_for("employees"))
        db.update_employee(
            emp_id,
            name=name,
            email=request.form.get("email", ""),
            phone=request.form.get("phone", ""),
            role=request.form.get("role", ""),
        )
        flash("Employee updated.", "success")
        return redirect(url_for("employees"))

    @app.route("/employees/<int:emp_id>/delete", methods=["POST"])
    def delete_employee(emp_id: int):
        emp = db.get_employee(emp_id)
        db.delete_employee(emp_id)
        flash(f"Removed {emp['name'] if emp else 'employee'}.", "success")
        return redirect(url_for("employees"))

    # ------------------------------------------------------------------ #
    # Schedule / shifts
    # ------------------------------------------------------------------ #
    @app.route("/schedule")
    def schedule():
        employees = db.list_employees()
        shifts = db.list_all_shifts()
        return render_template("schedule.html", employees=employees, shifts=shifts)

    @app.route("/schedule/add", methods=["POST"])
    def add_shift():
        try:
            employee_id = int(request.form.get("employee_id", ""))
        except ValueError:
            flash("Please choose an employee.", "error")
            return redirect(url_for("schedule"))

        work_date = request.form.get("work_date", "").strip()
        start_time = request.form.get("start_time", "").strip()
        end_time = request.form.get("end_time", "").strip()
        if not (work_date and start_time and end_time):
            flash("Date, start time and end time are all required.", "error")
            return redirect(url_for("schedule"))

        db.add_shift(
            employee_id=employee_id,
            work_date=work_date,
            start_time=start_time,
            end_time=end_time,
            location=request.form.get("location", ""),
            notes=request.form.get("notes", ""),
        )
        flash("Shift added.", "success")
        return redirect(url_for("schedule"))

    @app.route("/schedule/<int:shift_id>/delete", methods=["POST"])
    def delete_shift(shift_id: int):
        db.delete_shift(shift_id)
        flash("Shift removed.", "success")
        return redirect(url_for("schedule"))

    # ------------------------------------------------------------------ #
    # Sending schedules
    # ------------------------------------------------------------------ #
    def _send_to_employee(emp_id: int, channels: set[str]) -> list[str]:
        """Send one employee's upcoming schedule over the chosen channels.

        Returns a list of human-readable result messages.
        """
        emp = db.get_employee(emp_id)
        if emp is None:
            return ["Employee not found."]

        shifts = db.list_shifts_for_employee(emp_id, from_date=db.today_iso())
        text_body = notify.build_schedule_text(emp["name"], shifts)
        results: list[str] = []

        if "email" in channels:
            html_body = notify.build_schedule_html(emp["name"], shifts)
            ok, msg = notify.send_email(
                emp["email"],
                subject="Your upcoming work schedule",
                text_body=text_body,
                html_body=html_body,
            )
            results.append(f"{emp['name']} (email): {msg}")

        if "sms" in channels:
            ok, msg = notify.send_sms(emp["phone"], text_body)
            results.append(f"{emp['name']} (SMS): {msg}")

        return results

    def _channels_from_form() -> set[str]:
        channels = set(request.form.getlist("channels"))
        # Fall back to a single-channel button if no checkboxes were used.
        single = request.form.get("channel")
        if single:
            channels.add(single)
        return channels

    @app.route("/send/<int:emp_id>", methods=["POST"])
    def send_one(emp_id: int):
        channels = _channels_from_form()
        if not channels:
            flash("Choose at least one channel (email or SMS).", "error")
            return redirect(request.referrer or url_for("index"))
        for line in _send_to_employee(emp_id, channels):
            category = "success" if "sent" in line.lower() else "error"
            flash(line, category)
        return redirect(request.referrer or url_for("index"))

    @app.route("/send-all", methods=["POST"])
    def send_all():
        channels = _channels_from_form()
        if not channels:
            flash("Choose at least one channel (email or SMS).", "error")
            return redirect(url_for("index"))
        any_sent = False
        for emp in db.list_employees():
            for line in _send_to_employee(emp["id"], channels):
                category = "success" if "sent" in line.lower() else "error"
                if category == "success":
                    any_sent = True
                flash(line, category)
        if not any_sent:
            flash("No messages were sent -- check your delivery settings.", "error")
        return redirect(url_for("index"))

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=True)
