import os
import subprocess
import hashlib

# Hardcoded credentials — bandit B105/B106
SECRET_KEY = "hardcoded-super-secret-12345"
DB_PASSWORD = "admin123"

# SQL injection via string formatting — semgrep will flag this
def get_user(user_id):
    query = f"SELECT * FROM users WHERE id = {user_id}"
    return query

def get_user_by_name(name):
    query = "SELECT * FROM users WHERE name = '" + name + "'"
    return query

# Command injection — bandit B602
def run_command(user_input):
    subprocess.call(user_input, shell=True)
    os.system(user_input)

# Use of eval — semgrep eval-detected
def calculate(expression):
    return eval(expression)

# Weak hashing — bandit B303
def hash_password(password):
    return hashlib.md5(password.encode()).hexdigest()

# Insecure random
import random
def generate_token():
    return random.randint(100000, 999999)