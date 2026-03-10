#!/usr/bin/env python3
import subprocess, sys, os, shutil, time

def install_package(package):
    print('  [INSTALL] Installing ' + package + '...')
    r = subprocess.run(
        [sys.executable, '-m', 'pip', 'install', package,
         '--ignore-installed', '--break-system-packages', '--quiet'],
        capture_output=True, text=True)
    if r.returncode != 0:
        subprocess.run(
            [sys.executable, '-m', 'pip', 'install', package,
             '--break-system-packages', '--quiet'],
            capture_output=True, text=True)

print('\n[BOOT] Checking required Python packages...')
try:
    from google import genai
    print('  [OK]  google-genai already installed')
except ImportError:
    install_package('google-genai')
    from google import genai
    print('  [OK]  google-genai installed')

try:
    import psycopg2
    print('  [OK]  psycopg2 already installed')
except ImportError:
    install_package('psycopg2-binary')
    print('  [OK]  psycopg2-binary installed')

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
gemini_client  = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

DB_NAME = 'airflow_db'
DB_USER = 'airflow_user'
DB_PASS = 'airflow_pass'
DB_HOST = 'localhost'
DB_PORT = '5432'

def pg_conn_str():
    return 'postgresql+psycopg2://' + DB_USER + ':' + DB_PASS + '@' + DB_HOST + ':' + DB_PORT + '/' + DB_NAME

def print_banner():
    print('\n' + '='*52)
    print('   Airflow Configuration Agent')
    print('   Powered by Google Gemini')
    print('='*52)

def print_step(n, total, msg):
    print('\n[' + str(n) + '/' + str(total) + '] => ' + msg)
    print('-'*48)

def print_status(msg, status='info'):
    icons = {'info':'[INFO]','ok':'[OK]  ','warn':'[WARN]','error':'[ERR] ','run':'[RUN] ','fix':'[FIX] ','llm':'[LLM] '}
    print('  ' + icons.get(status,'[    ]') + '  ' + msg)

def run_command(command, env=None):
    print_status('Running: ' + command, 'run')
    result = subprocess.run(command, shell=True, capture_output=True,
                            text=True, env=env or os.environ.copy())
    return result.returncode == 0, result.stdout + result.stderr

def ask_gemini_to_fix(error_output, command):
    print_status('Sending error to Gemini for analysis...', 'llm')
    if not gemini_client:
        return ''
    prompt = ('I ran this command to set up Apache Airflow:\n\nCOMMAND:\n' + command +
              '\n\nERROR OUTPUT:\n' + error_output +
              '\n\n1. Briefly explain what went wrong.\n2. Provide exact bash commands to fix it.\nPut commands inside a bash code block.')
    for attempt in range(3):
        try:
            response = gemini_client.models.generate_content(model='gemini-2.0-flash', contents=prompt)
            return response.text
        except Exception as e:
            err = str(e)
            if '429' in err or 'quota' in err.lower():
                wait = 20 * (attempt + 1)
                print_status('Gemini quota hit. Waiting ' + str(wait) + 's...', 'warn')
                time.sleep(wait)
            else:
                print_status('Gemini error: ' + str(e), 'error')
                return ''
    print_status('Gemini quota exhausted. Skipping LLM fix.', 'warn')
    return ''

def extract_commands_from_llm(llm_response):
    commands, in_block = [], False
    for line in llm_response.splitlines():
        s = line.strip()
        if s.startswith('```bash'):
            in_block = True
            continue
        elif s == '```' and in_block:
            in_block = False
            continue
        if in_block and s:
            commands.append(s)
    return commands

def run_with_llm_retry(command, env=None, max_retries=2):
    success, output = run_command(command, env)
    if success:
        print_status('Command succeeded!', 'ok')
        return True
    for attempt in range(1, max_retries + 1):
        print_status('Command failed (retry ' + str(attempt) + '/' + str(max_retries) + ')', 'error')
        for line in output.strip().splitlines():
            print('      ' + line)
        fix_response = ask_gemini_to_fix(output, command)
        if fix_response:
            for line in fix_response.splitlines():
                if '```' not in line and line.strip():
                    print_status('Gemini: ' + line.strip(), 'llm')
                    break
        fix_commands = extract_commands_from_llm(fix_response)
        if not fix_commands:
            print_status('No fix commands from Gemini. Skipping.', 'warn')
            return False
        print_status('Applying Gemini fix commands...', 'fix')
        all_ok = True
        for fix_cmd in fix_commands:
            ok, out = run_command(fix_cmd, env)
            if not ok:
                output = out
                all_ok = False
                break
        if all_ok:
            print_status('Gemini fix applied successfully!', 'ok')
            return True
    print_status('Could not recover. Please check manually.', 'error')
    return False

def check_python():
    print_step(1, 7, 'Checking Python installation')
    python_exec = shutil.which('python3') or shutil.which('python')
    if python_exec:
        result = subprocess.run([python_exec, '--version'], capture_output=True, text=True)
        version_str = (result.stdout + result.stderr).strip()
        print_status('Python found: ' + version_str, 'ok')
        try:
            parts = version_str.replace('Python ', '').split('.')
            major, minor = int(parts[0]), int(parts[1])
            if major == 3 and minor >= 8:
                print_status('Python version compatible (3.8+)', 'ok')
                return python_exec
        except Exception:
            return python_exec
    run_with_llm_retry('apt-get update -qq && apt-get install -y python3.10 python3-pip')
    return shutil.which('python3') or 'python3'

def setup_postgresql():
    print_step(2, 7, 'Setting up PostgreSQL')
    if not shutil.which('psql'):
        print_status('PostgreSQL not found. Installing...', 'warn')
        ok = run_with_llm_retry('apt-get update -qq && apt-get install -y postgresql postgresql-contrib')
        if not ok:
            print_status('PostgreSQL installation failed.', 'error')
            return False
        print_status('PostgreSQL installed!', 'ok')
    else:
        print_status('PostgreSQL already installed.', 'ok')
    print_status('Starting PostgreSQL service...', 'info')
    run_command('service postgresql start')
    time.sleep(3)
    print_status('PostgreSQL is running!', 'ok')
    sql_check = 'sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname=' + chr(39) + DB_NAME + chr(39) + '"'
    check = subprocess.run(sql_check, shell=True, capture_output=True, text=True)
    if '1' in check.stdout:
        print_status('Database already exists. Skipping.', 'ok')
    else:
        print_status('Creating database and user...', 'info')
        run_with_llm_retry('sudo -u postgres psql -c "CREATE DATABASE ' + DB_NAME + ';"')
        run_with_llm_retry('sudo -u postgres psql -c "CREATE USER ' + DB_USER + ' WITH PASSWORD ' + chr(39) + DB_PASS + chr(39) + ';"')
        run_with_llm_retry('sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE ' + DB_NAME + ' TO ' + DB_USER + ';"')
        run_with_llm_retry('sudo -u postgres psql -c "ALTER DATABASE ' + DB_NAME + ' OWNER TO ' + DB_USER + ';"')
        print_status('Database and user created!', 'ok')
    run_command(sys.executable + ' -m pip install psycopg2-binary --ignore-installed --break-system-packages --quiet')
    print_status('psycopg2-binary ready!', 'ok')
    return True

def check_and_install_airflow(python_exec, env):
    print_step(3, 7, 'Checking Apache Airflow installation')
    if shutil.which('airflow'):
        result = subprocess.run('airflow version', shell=True, capture_output=True, text=True, env=env)
        ver = (result.stdout + result.stderr).strip().splitlines()
        print_status('Airflow already installed: ' + (ver[0] if ver else 'unknown'), 'ok')
        return True
    print_status('Installing Airflow 2.9.3... (3-5 mins)', 'run')
    py_result = subprocess.run([python_exec, '--version'], capture_output=True, text=True)
    py_str = (py_result.stdout + py_result.stderr).strip()
    try:
        parts = py_str.replace('Python ', '').split('.')
        py_ver = parts[0] + '.' + parts[1]
    except Exception:
        py_ver = '3.10'
    constraints_url = 'https://raw.githubusercontent.com/apache/airflow/constraints-2.9.3/constraints-' + py_ver + '.txt'
    install_cmd = (python_exec + ' -m pip install apache-airflow==2.9.3 --constraint ' +
                   chr(34) + constraints_url + chr(34) +
                   ' --ignore-installed --break-system-packages --quiet')
    ok = run_with_llm_retry(install_cmd, env=env)
    if ok:
        print_status('Airflow 2.9.3 installed!', 'ok')
    return ok

def init_airflow_db(env):
    print_step(4, 7, 'Initializing Airflow Database on PostgreSQL')
    print_status('Connecting to: ' + pg_conn_str(), 'info')
    ok, out = run_command('airflow db migrate', env)
    if not ok:
        ok = run_with_llm_retry('airflow db init', env=env)
    else:
        print_status('Airflow DB ready on PostgreSQL!', 'ok')
    return ok

def create_airflow_user(username, password, env):
    print_step(5, 7, 'Creating Airflow Admin User')
    check = subprocess.run('airflow users list', shell=True, capture_output=True, text=True, env=env)
    if username in check.stdout:
        print_status('User already exists. Skipping.', 'ok')
        return True
    cmd = ('airflow users create --username ' + username +
           ' --password ' + password +
           ' --firstname Admin --lastname User --role Admin --email ' + username + '@airflow.local')
    ok = run_with_llm_retry(cmd, env=env)
    if ok:
        print_status('User created!', 'ok')
    return ok

def start_webserver(port, env):
    print_step(6, 7, 'Starting Airflow Webserver on port ' + port)
    subprocess.run('pkill -f airflow webserver 2>/dev/null', shell=True)
    time.sleep(2)
    ok = run_with_llm_retry('airflow webserver --port ' + port + ' --daemon', env=env)
    if ok:
        print_status('Webserver running on http://localhost:' + port, 'ok')
    return ok

def start_scheduler(env):
    print_step(7, 7, 'Starting Airflow Scheduler')
    subprocess.run('pkill -f airflow scheduler 2>/dev/null', shell=True)
    time.sleep(2)
    ok = run_with_llm_retry('airflow scheduler --daemon', env=env)
    if ok:
        print_status('Scheduler running!', 'ok')
    return ok

def collect_config():
    print('\n  Please provide the following details:\n')
    port = input('  Webserver Port [default: 8080] : ').strip() or '8080'
    username = input('  Admin Username               : ').strip()
    while not username:
        print('  Username cannot be empty.')
        username = input('  Admin Username               : ').strip()
    password = input('  Admin Password               : ').strip()
    while not password:
        print('  Password cannot be empty.')
        password = input('  Admin Password               : ').strip()
    return {'port': port, 'username': username, 'password': password}

def print_summary(config):
    print('\n+--------------------------------------+')
    print('|        Configuration Summary         |')
    print('+--------------------------------------+')
    print('|  Port     : ' + config['port'].ljust(26) + '|')
    print('|  Username : ' + config['username'].ljust(26) + '|')
    print('|  Password : ' + ('*' * len(config['password'])).ljust(26) + '|')
    print('|  Database : PostgreSQL               |')
    print('|  DB Name  : ' + DB_NAME.ljust(26) + '|')
    print('+--------------------------------------+')

def print_final_output(config):
    print('\n' + '='*52)
    print('   AIRFLOW IS UP AND RUNNING!')
    print('='*52)
    print('\n  URL      : http://localhost:' + config['port'])
    print('  Username : ' + config['username'])
    print('  Password : ' + config['password'])
    print('  Database : PostgreSQL (' + DB_NAME + ')')
    print('  DB User  : ' + DB_USER)
    print('  DB Pass  : ' + DB_PASS)
    print('\n  Airflow Home : ~/airflow')
    print('  Logs         : ~/airflow/logs')
    print('\n  To stop:')
    print('    pkill -f airflow')
    print('\n' + '='*52 + '\n')

def main():
    print_banner()
    if not os.environ.get('GEMINI_API_KEY'):
        print_status('GEMINI_API_KEY is not set!', 'error')
        print_status('Get free key: https://aistudio.google.com/app/apikey', 'info')
        print_status('Run: export GEMINI_API_KEY=your-key-here', 'info')
        sys.exit(1)
    print_status('Gemini API key found', 'ok')
    print('\nWhat would you like to do?')
    print('  1. Configure Apache Airflow with PostgreSQL')
    print('  2. Exit')
    choice = input('\nEnter your choice (1 or 2): ').strip()
    if choice == '2':
        print('\nGoodbye!\n')
        sys.exit(0)
    elif choice != '1':
        print_status('Invalid choice.', 'warn')
        sys.exit(1)
    config = collect_config()
    print_summary(config)
    confirm = input('\n  Proceed? (yes/no): ').strip().lower()
    if confirm not in ('yes', 'y'):
        sys.exit(0)
    print('\nStarting full automated setup...\n')
    airflow_home = os.path.join(os.path.expanduser('~'), 'airflow')
    os.makedirs(airflow_home, exist_ok=True)
    env = os.environ.copy()
    env['AIRFLOW_HOME']                        = airflow_home
    env['AIRFLOW__DATABASE__SQL_ALCHEMY_CONN'] = pg_conn_str()
    env['AIRFLOW__CORE__EXECUTOR']             = 'LocalExecutor'
    env['AIRFLOW__CORE__LOAD_EXAMPLES']        = 'False'
    python_exec = check_python()
    if not setup_postgresql(): sys.exit(1)
    if not check_and_install_airflow(python_exec, env): sys.exit(1)
    if not init_airflow_db(env): sys.exit(1)
    if not create_airflow_user(config['username'], config['password'], env): sys.exit(1)
    start_webserver(config['port'], env)
    start_scheduler(env)
    print_final_output(config)

if __name__ == '__main__':
    main()
