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
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute('SELECT * FROM students')
        students = cur.fetchall()

        data = []
        for student in students:
            adm = student['admission_no']
            cur.execute('SELECT fee_amount FROM fee_structure WHERE class = ?', (student['class'],))
            fee_row = cur.fetchone()
            total_fee = fee_row['fee_amount'] if fee_row else 0
            cur.execute('SELECT SUM(amount_paid) FROM payments WHERE admission_no = ?', (adm,))
            paid_row = cur.fetchone()
            total_paid = paid_row[0] if paid_row[0] else 0

            data.append({
                'admission_no': adm,
                'name': student['name'],
                'class': student['class'],
                'phone': student['phone'],
                'total_fee': total_fee,
                'total_paid': total_paid,
                'due': total_fee - total_paid
            })

    return render_template('index.html', students=data)


@app.route('/add_student', methods=['GET', 'POST'])
def add_student():
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
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute('SELECT * FROM students')
        students = cur.fetchall()

        reminders_sent = 0
        for student in students:
            adm = student['admission_no']
            phone = student['phone']
            cur.execute('SELECT fee_amount FROM fee_structure WHERE class = ?', (student['class'],))
            fee_row = cur.fetchone()
            total_fee = fee_row['fee_amount'] if fee_row else 0
            cur.execute('SELECT SUM(amount_paid) FROM payments WHERE admission_no = ?', (adm,))
            paid_row = cur.fetchone()
            total_paid = paid_row[0] if paid_row[0] else 0
            due = total_fee - total_paid

            if due > 0:
                message = f"Dear {student['name']}, you have a fee due of ${due:.2f}. Please pay promptly."
                send_sms(phone, message)
                reminders_sent += 1

        flash(f"Reminders sent to {reminders_sent} student(s).", "success")

    return redirect(url_for('index'))


@app.route('/set_fee', methods=['GET', 'POST'])
def set_fee():
    if request.method == 'POST':
        class_name = request.form['class_name']
        fee_amount = request.form['fee_amount']

        if not class_name or not fee_amount:
            flash('All fields are required.', 'error')
            return redirect(url_for('set_fee'))

        with get_db_connection() as conn:
            conn.execute(
                'REPLACE INTO fee_structure (class, fee_amount) VALUES (?, ?)',
                (class_name, float(fee_amount))
            )
            conn.commit()

        flash('Fee set successfully!', 'success')
        return redirect(url_for('set_fee'))

    return render_template('set_fee.html')


@app.route('/record_payment', methods=['GET', 'POST'])
def record_payment():
    if request.method == 'POST':
        admission_no = request.form['admission_no']
        amount_paid = float(request.form['amount'])

        with get_db_connection() as conn:
            student = conn.execute(
                'SELECT * FROM students WHERE admission_no = ?', (admission_no,)
            ).fetchone()

            if not student:
                flash('Student not found.', 'error')
                return redirect(url_for('record_payment'))

            date = datetime.now().strftime('%Y-%m-%d')
            conn.execute(
                'INSERT INTO payments (admission_no, amount_paid, payment_date) VALUES (?, ?, ?)',
                (admission_no, amount_paid, date)
            )
            conn.commit()

        flash('Payment recorded successfully!', 'success')
        return redirect(url_for('receipt', admission_no=admission_no, amount_paid=amount_paid, date=date))

    return render_template('record_payment.html')


@app.route('/check_due', methods=['GET', 'POST'])
def check_due():
    student = None
    dues = []

    if request.method == 'POST':
        admission_no = request.form.get('admission_no', '').strip()

        if admission_no:
            with get_db_connection() as conn:
                c = conn.cursor()
                c.execute('SELECT * FROM students WHERE admission_no = ?', (admission_no,))
                student = c.fetchone()

                c.execute('SELECT * FROM fee_cycles WHERE admission_no = ?', (admission_no,))
                dues = c.fetchall()

            if not student:
                flash('Student not found.', 'error')
                return redirect(url_for('check_due'))

    return render_template('check_due.html', student=student, dues=dues)


@app.route('/receipt')
def receipt():
    admission_no = request.args.get('admission_no')
    amount_paid = float(request.args.get('amount_paid'))
    date = request.args.get('date')

    with get_db_connection() as conn:
        student = conn.execute(
            'SELECT * FROM students WHERE admission_no = ?', (admission_no,)
        ).fetchone()
        total_paid = get_total_paid(admission_no)
        total_fee = get_total_fee(admission_no)
        due_amount = get_due_amount(admission_no)

    return render_template('receipt.html',
                           student=student,
                           amount_paid=amount_paid,
                           date=date,
                           total_paid=total_paid,
                           total_fee=total_fee,
                           due_amount=due_amount)

from flask import request, render_template, redirect, url_for, flash

@app.route('/set_due_date', methods=['GET', 'POST'])
def set_due_date():
    if request.method == 'POST':
        due_date = request.form.get('due_date')

        if not due_date:
            flash('Please select a due date.', 'danger')
            return redirect(url_for('set_due_date'))

        # TODO: Save due_date to database or config
        flash(f'Due date set to {due_date}', 'success')
        return redirect(url_for('index'))

    return render_template('set_due_date.html')


if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
