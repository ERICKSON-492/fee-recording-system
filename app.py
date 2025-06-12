"""Fee Management System Application using Flask"""

import os
import re
import sqlite3
import logging
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev_secret_key')
DATABASE = 'school_fees.db'

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
PHONE_REGEX = re.compile(r'^\+?[1-9]\d{7,14}$')


def init_db():
    """Initialize the database with necessary tables."""
    with sqlite3.connect(DATABASE) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS students (
                        admission_no TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        class TEXT NOT NULL,
                        phone TEXT NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS fee_structure (
                        class TEXT PRIMARY KEY,
                        fee_amount REAL NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS payments (
                        payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        admission_no TEXT NOT NULL,
                        amount_paid REAL NOT NULL,
                        payment_date TEXT NOT NULL,
                        FOREIGN KEY(admission_no) REFERENCES students(admission_no))''')
        c.execute('''CREATE TABLE IF NOT EXISTS fee_cycles (
                        cycle_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        admission_no TEXT NOT NULL,
                        due_date TEXT NOT NULL,
                        FOREIGN KEY(admission_no) REFERENCES students(admission_no))''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_payments_admission ON payments(admission_no)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_fee_cycles_admission ON fee_cycles(admission_no)')
        conn.commit()


def get_db_connection():
    """Establish and return a database connection."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def is_valid_phone(phone):
    """Check if a phone number matches the expected format."""
    return bool(PHONE_REGEX.match(phone))


def send_sms(to_phone, message):
    """Send an SMS using Twilio."""
    sid = os.getenv('TWILIO_ACCOUNT_SID')
    token = os.getenv('TWILIO_AUTH_TOKEN')
    twilio_phone = os.getenv('TWILIO_PHONE_NUMBER')

    if not all([sid, token, twilio_phone]):
        logger.error("Missing Twilio credentials.")
        flash("Missing Twilio credentials.", "error")
        return False

    if not is_valid_phone(to_phone):
        flash("Invalid phone format.", "error")
        return False

    try:
        Client(sid, token).messages.create(
            body=message, from_=twilio_phone, to=to_phone
        )
        return True
    except TwilioRestException as e:
        logger.error("SMS send failed: %s", e)
        flash("SMS failed due to server error.", "error")
        return False


def get_total_fee(adm):
    """Get total fee amount for a student based on class."""
    with get_db_connection() as conn:
        student = conn.execute(
            'SELECT class FROM students WHERE admission_no=?', (adm,)
        ).fetchone()
        if not student:
            return None
        fee = conn.execute(
            'SELECT fee_amount FROM fee_structure WHERE class=?', (student['class'],)
        ).fetchone()
        return fee['fee_amount'] if fee else None


def get_total_paid(adm):
    """Get total amount paid by a student."""
    with get_db_connection() as conn:
        total = conn.execute(
            'SELECT SUM(amount_paid) as total_paid FROM payments WHERE admission_no=?', (adm,)
        ).fetchone()
        return total['total_paid'] or 0.0


def get_due_amount(adm):
    """Calculate the due amount for a student, including late fees."""
    total_fee = get_total_fee(adm)
    if total_fee is None:
        return None
    total_paid = get_total_paid(adm)
    late_fee = 0.0
    today = datetime.now()

    with get_db_connection() as conn:
        cycles = conn.execute(
            'SELECT due_date FROM fee_cycles WHERE admission_no=?', (adm,)
        ).fetchall()

    for cycle in cycles:
        try:
            due_date = datetime.strptime(cycle['due_date'], '%Y-%m-%d')
        except ValueError:
            continue
        if today > due_date:
            weeks = (today - due_date).days // 7
            if weeks > 0 and total_fee - total_paid > 0:
                late_fee += (total_fee - total_paid) * 0.02 * weeks

    total_due = (total_fee + late_fee) - total_paid
    return total_due if total_due > 0 else 0


@app.route('/')
def index():
    """Render the homepage with student list and due info."""
    with get_db_connection() as conn:
        students = conn.execute('SELECT * FROM students').fetchall()
    data = []
    for s in students:
        data.append({
            'admission_no': s['admission_no'],
            'name': s['name'],
            'class': s['class'],
            'phone': s['phone'],
            'due_amount': get_due_amount(s['admission_no'])
        })
    return render_template('index.html', students=data)


@app.route('/add_student', methods=['GET', 'POST'])
def add_student():
    """Add a new student to the database."""
    if request.method == 'POST':
        adm = request.form['admission_no'].strip()
        name = request.form['name'].strip()
        cls = request.form['class_name'].strip()
        phone = request.form['phone'].strip()

        if not all([adm, name, cls, phone]):
            flash('All fields are required.', 'error')
            return redirect(url_for('add_student'))

        if not is_valid_phone(phone):
            flash('Invalid phone format.', 'error')
            return redirect(url_for('add_student'))

        try:
            with get_db_connection() as conn:
                conn.execute(
                    'INSERT INTO students (admission_no, name, class, phone) VALUES (?, ?, ?, ?)',
                    (adm, name, cls, phone)
                )
                conn.commit()
            flash('Student added.', 'success')
        except sqlite3.IntegrityError:
            flash('Admission number exists.', 'error')

        return redirect(url_for('index'))

    return render_template('add_student.html')


@app.route('/send_reminders')
def send_reminders():
    """Send SMS reminders to students with outstanding fees."""
    with get_db_connection() as conn:
        students = conn.execute('SELECT admission_no, name, phone FROM students').fetchall()

    count = 0
    for s in students:
        due = get_due_amount(s['admission_no'])
        if due and due > 0:
            msg = (
                f"Dear Parent, student {s['name']} (Adm: {s['admission_no']}) "
                f"owes ${due:.2f}. Kindly pay soon."
            )
            if send_sms(s['phone'], msg):
                count += 1

    flash(f'Reminders sent to {count} students.', 'success')
    return redirect(url_for('index'))


if __name__ == '__main__':
    init_db()
    app.run(debug=True)
