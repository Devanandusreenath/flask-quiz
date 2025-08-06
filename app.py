from flask import Flask, request, jsonify, session, render_template_string
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room
import pymysql
import hashlib
import secrets
from datetime import datetime, timedelta
import json
import os
from functools import wraps
import sys
import time

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', secrets.token_hex(16))

# Enable CORS for all domains
CORS(app, supports_credentials=True)

# Initialize SocketIO for real-time features - Fixed async_mode
socketio = SocketIO(app, cors_allowed_origins="*")

# Database configuration - Updated with better error handling
DB_CONFIG = {
    'host': os.getenv('MYSQLHOST', 'localhost'),
    'user': os.getenv('MYSQLUSER', 'root'),
    'password': os.getenv('MYSQL_ROOT_PASSWORD', ''),
    'database': os.getenv('MYSQL_DATABASE', 'buzzer_quiz_game'),
    'port': int(os.getenv('MYSQLPORT', 3306))
}

# Print database config for debugging (without password)
print("Database Configuration:")
print(f"Host: {DB_CONFIG['host']}")
print(f"User: {DB_CONFIG['user']}")
print(f"Database: {DB_CONFIG['database']}")
print(f"Port: {DB_CONFIG['port']}")

# Global game state
game_state = {
    'active_games': {},
    'connected_users': {},
    'current_question': None,
    'timer_active': False,
    'buzzed_player': None
}

def get_db_connection():
    """Create and return a database connection with retry logic"""
    max_retries = 3
    retry_delay = 1
    
    for attempt in range(max_retries):
        try:
            connection = pymysql.connect(
                host=DB_CONFIG['host'],
                user=DB_CONFIG['user'],
                password=DB_CONFIG['password'],
                database=DB_CONFIG.get('database'),
                charset='utf8mb4',
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True,
                connect_timeout=10,
                read_timeout=10,
                write_timeout=10
            )
            return connection
        except Exception as e:
            print(f"Database connection attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
            else:
                print(f"Failed to connect to MySQL after {max_retries} attempts: {e}")
                return None

def init_database():
    """Initialize the database and create tables with better error handling"""
    print("Starting database initialization...")
    
    try:
        # First connect without database to create it
        temp_config = DB_CONFIG.copy()
        database_name = temp_config.pop('database', 'buzzer_quiz_game')
        
        print(f"Connecting to MySQL server at {temp_config['host']}...")
        connection = pymysql.connect(
            host=temp_config['host'],
            user=temp_config['user'],
            password=temp_config['password'],
            charset='utf8mb4',
            connect_timeout=10
        )
        cursor = connection.cursor()
        
        # Create database if it doesn't exist
        print(f"Creating database '{database_name}' if it doesn't exist...")
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{database_name}`")
        cursor.close()
        connection.close()
        print("Database created successfully or already exists")
        
        # Now connect with the database
        print("Connecting to the database...")
        connection = get_db_connection()
        if not connection:
            print("Failed to connect to database after creation")
            return False
            
        cursor = connection.cursor()
        
        print("Creating tables...")
        
        # Create users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(50) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                role ENUM('admin', 'player') DEFAULT 'player',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                total_score INT DEFAULT 0,
                games_played INT DEFAULT 0
            )
        """)
        print("Users table created")
        
        # Create quizzes table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS quizzes (
                id INT AUTO_INCREMENT PRIMARY KEY,
                title VARCHAR(255) NOT NULL,
                created_by INT,
                correct_points INT DEFAULT 10,
                wrong_points INT DEFAULT 5,
                time_per_question INT DEFAULT 30,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (created_by) REFERENCES users(id)
            )
        """)
        print("Quizzes table created")
        
        # Create questions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS questions (
                id INT AUTO_INCREMENT PRIMARY KEY,
                quiz_id INT,
                question_text TEXT NOT NULL,
                question_type ENUM('mcq', 'open') DEFAULT 'mcq',
                options JSON,
                correct_answer VARCHAR(255),
                question_order INT,
                FOREIGN KEY (quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE
            )
        """)
        print("Questions table created")
        
        # Create game_sessions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS game_sessions (
                id INT AUTO_INCREMENT PRIMARY KEY,
                quiz_id INT,
                host_id INT,
                session_code VARCHAR(10) UNIQUE,
                status ENUM('waiting', 'active', 'completed') DEFAULT 'waiting',
                current_question INT DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (quiz_id) REFERENCES quizzes(id),
                FOREIGN KEY (host_id) REFERENCES users(id)
            )
        """)
        print("Game sessions table created")
        
        # Create game_participants table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS game_participants (
                id INT AUTO_INCREMENT PRIMARY KEY,
                session_id INT,
                user_id INT,
                current_score INT DEFAULT 0,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES game_sessions(id),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        print("Game participants table created")
        
        # Create buzz_log table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS buzz_log (
                id INT AUTO_INCREMENT PRIMARY KEY,
                session_id INT,
                user_id INT,
                question_id INT,
                buzz_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                was_correct BOOLEAN,
                points_awarded INT,
                FOREIGN KEY (session_id) REFERENCES game_sessions(id),
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (question_id) REFERENCES questions(id)
            )
        """)
        print("Buzz log table created")
        
        # Check if admin user already exists
        cursor.execute("SELECT id FROM users WHERE username = 'admin'")
        admin_exists = cursor.fetchone()
        
        if not admin_exists:
            # Insert default admin user
            admin_password = hashlib.sha256("admin123".encode()).hexdigest()
            cursor.execute("""
                INSERT INTO users (username, password_hash, role) 
                VALUES ('admin', %s, 'admin')
            """, (admin_password,))
            print("Default admin user created")
        else:
            print("Admin user already exists")
        
        cursor.close()
        connection.close()
        
        print("Database initialized successfully!")
        return True
        
    except Exception as e:
        print(f"Error initializing database: {e}")
        import traceback
        traceback.print_exc()
        return False

def hash_password(password):
    """Hash a password using SHA256"""
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password, hashed):
    """Verify a password against its hash"""
    return hashlib.sha256(password.encode()).hexdigest() == hashed

def login_required(f):
    """Decorator to require login for protected routes"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'success': False, 'message': 'Login required'}), 401
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    """Decorator to require admin role"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'success': False, 'message': 'Login required'}), 401
        
        connection = get_db_connection()
        if not connection:
            return jsonify({'success': False, 'message': 'Database error'}), 500
        
        cursor = connection.cursor()
        cursor.execute("SELECT role FROM users WHERE id = %s", (session['user_id'],))
        result = cursor.fetchone()
        cursor.close()
        connection.close()
        
        if not result or result['role'] != 'admin':
            return jsonify({'success': False, 'message': 'Admin access required'}), 403
        
        return f(*args, **kwargs)
    return decorated_function

# Read the HTML template
def get_html_template():
    """Return the HTML template as a string"""
    return '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ultimate Buzzer Quiz Game</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.js"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            color: white;
        }

        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }

        .screen {
            display: none;
            animation: fadeIn 0.5s ease-in;
        }

        .screen.active {
            display: block;
        }

        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(20px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .card {
            background: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 30px;
            margin: 20px 0;
            border: 1px solid rgba(255, 255, 255, 0.2);
            box-shadow: 0 8px 32px 0 rgba(31, 38, 135, 0.37);
        }

        h1, h2 {
            text-align: center;
            margin-bottom: 30px;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
        }

        h1 {
            font-size: 3em;
            background: linear-gradient(45deg, #FFD700, #FFA500);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }

        .form-group {
            margin-bottom: 20px;
        }

        label {
            display: block;
            margin-bottom: 8px;
            font-weight: bold;
        }

        input, select, textarea {
            width: 100%;
            padding: 12px;
            border: none;
            border-radius: 10px;
            background: rgba(255, 255, 255, 0.9);
            color: #333;
            font-size: 16px;
        }

        textarea {
            resize: vertical;
            min-height: 80px;
        }

        .btn {
            background: linear-gradient(45deg, #FF6B6B, #4ECDC4);
            color: white;
            border: none;
            padding: 12px 24px;
            border-radius: 25px;
            cursor: pointer;
            font-size: 16px;
            font-weight: bold;
            transition: all 0.3s ease;
            margin: 5px;
        }

        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(0,0,0,0.3);
        }

        .btn-primary {
            background: linear-gradient(45deg, #667eea, #764ba2);
        }

        .btn-success {
            background: linear-gradient(45deg, #56ab2f, #a8e6cf);
        }

        .btn-danger {
            background: linear-gradient(45deg, #FF416C, #FF4B2B);
        }

        .btn-warning {
            background: linear-gradient(45deg, #f093fb, #f5576c);
        }

        .message {
            padding: 10px;
            margin: 10px 0;
            border-radius: 10px;
            text-align: center;
        }

        .message.success {
            background: rgba(76, 175, 80, 0.3);
            color: #4CAF50;
        }

        .message.error {
            background: rgba(244, 67, 54, 0.3);
            color: #F44336;
        }

        .loading {
            text-align: center;
            padding: 20px;
        }

        .spinner {
            border: 4px solid rgba(255, 255, 255, 0.3);
            border-radius: 50%;
            border-top: 4px solid #fff;
            width: 40px;
            height: 40px;
            animation: spin 1s linear infinite;
            margin: 0 auto;
        }

        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
    </style>
</head>
<body>
    <div class="container">
        <!-- Login Screen -->
        <div id="loginScreen" class="screen active">
            <div class="card">
                <h1>🎯 Ultimate Buzzer Quiz Game</h1>
                <div class="form-group">
                    <label>Username:</label>
                    <input type="text" id="username" placeholder="Enter username" value="admin">
                </div>
                <div class="form-group">
                    <label>Password:</label>
                    <input type="password" id="password" placeholder="Enter password" value="admin123">
                </div>
                <button class="btn btn-primary" onclick="login()">Login</button>
                <div id="loginMessage"></div>
                <div style="margin-top: 20px; padding: 15px; background: rgba(255, 255, 255, 0.1); border-radius: 10px;">
                    <h3>Default Login Credentials:</h3>
                    <p><strong>Admin:</strong> admin / admin123</p>
                    <p><em>Create player accounts from the admin dashboard</em></p>
                </div>
            </div>
        </div>

        <!-- Success Screen -->
        <div id="successScreen" class="screen">
            <div class="card">
                <h2>✅ Database Initialized Successfully!</h2>
                <p>Your Buzzer Quiz Game is now ready to use.</p>
                <button class="btn btn-primary" onclick="showScreen('loginScreen')">Go to Login</button>
            </div>
        </div>

        <!-- Error Screen -->
        <div id="errorScreen" class="screen">
            <div class="card">
                <h2>❌ Database Connection Error</h2>
                <p id="errorMessage">Unable to connect to the database. Please check your configuration.</p>
                <button class="btn btn-warning" onclick="checkDatabaseConnection()">Retry Connection</button>
            </div>
        </div>
    </div>

    <script>
        // Check database connection on page load
        window.onload = async function() {
            await checkDatabaseConnection();
        };

        async function checkDatabaseConnection() {
            showLoading('loginMessage');
            
            try {
                const response = await fetch('/api/health');
                const result = await response.json();
                
                if (result.status === 'healthy') {
                    showScreen('loginScreen');
                    showMessage('loginMessage', 'Database connected successfully! 🎉', 'success');
                } else {
                    throw new Error('Database not healthy');
                }
            } catch (error) {
                console.error('Database connection error:', error);
                document.getElementById('errorMessage').textContent = 
                    'Unable to connect to the database. Please check your Railway MySQL configuration.';
                showScreen('errorScreen');
            }
        }

        async function login() {
            const username = document.getElementById('username').value.trim();
            const password = document.getElementById('password').value;
            
            if (!username || !password) {
                showMessage('loginMessage', 'Please enter both username and password', 'error');
                return;
            }
            
            showLoading('loginMessage');
            
            try {
                const response = await fetch('/api/login', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    credentials: 'include',
                    body: JSON.stringify({ username, password })
                });
                
                const result = await response.json();
                
                if (result.success) {
                    showMessage('loginMessage', `Welcome ${result.user.username}! 🎮`, 'success');
                    setTimeout(() => {
                        if (result.user.role === 'admin') {
                            window.location.href = '/dashboard';
                        } else {
                            window.location.href = '/player';
                        }
                    }, 1500);
                } else {
                    showMessage('loginMessage', result.message, 'error');
                }
            } catch (error) {
                console.error('Login error:', error);
                showMessage('loginMessage', 'Network error. Please try again.', 'error');
            }
        }

        function showScreen(screenId) {
            document.querySelectorAll('.screen').forEach(screen => {
                screen.classList.remove('active');
            });
            document.getElementById(screenId).classList.add('active');
        }

        function showMessage(elementId, message, type) {
            const element = document.getElementById(elementId);
            if (element) {
                element.innerHTML = `<div class="message ${type}">${message}</div>`;
                setTimeout(() => {
                    if (element.innerHTML.includes(message)) {
                        element.innerHTML = '';
                    }
                }, 5000);
            }
        }

        function showLoading(elementId) {
            const element = document.getElementById(elementId);
            if (element) {
                element.innerHTML = `
                    <div class="loading">
                        <div class="spinner"></div>
                        <p>Connecting to database...</p>
                    </div>
                `;
            }
        }
    </script>
</body>
</html>'''

# Initialize database when the app starts (not just when script runs directly)
print("Initializing application...")
database_initialized = init_database()

if not database_initialized:
    print("WARNING: Database initialization failed!")
    print("The application may not work correctly.")
    print("Please check your database configuration and connection.")

# Routes
@app.route('/')
def home():
    return render_template_string(get_html_template())

@app.route('/dashboard')
def admin_dashboard():
    # You can create a separate admin dashboard HTML here
    return "<h1>Admin Dashboard - Coming Soon!</h1><a href='/'>Back to Home</a>"

@app.route('/player')
def player_screen():
    # You can create a separate player screen HTML here
    return "<h1>Player Screen - Coming Soon!</h1><a href='/'>Back to Home</a>"

@app.route('/api/info')
def api_info():
    return jsonify({
        'message': 'Buzzer Quiz Game API',
        'status': 'running',
        'version': '1.0.0',
        'database_initialized': database_initialized,
        'endpoints': {
            'health': '/api/health',
            'login': '/api/login',
            'register': '/api/register',
            'quizzes': '/api/quizzes',
            'players': '/api/players',
            'sessions': '/api/sessions',
            'stats': '/api/stats'
        }
    })

@app.route('/api/health', methods=['GET'])
def health_check():
    # Test database connection
    connection = get_db_connection()
    if connection:
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
            connection.close()
            return jsonify({
                'status': 'healthy', 
                'message': 'Server and database are running',
                'database_initialized': database_initialized
            })
        except Exception as e:
            return jsonify({
                'status': 'unhealthy', 
                'message': f'Database query failed: {str(e)}',
                'database_initialized': database_initialized
            }), 500
    else:
        return jsonify({
            'status': 'unhealthy', 
            'message': 'Cannot connect to database',
            'database_initialized': database_initialized
        }), 500

@app.route('/api/login', methods=['POST'])
def login():
    try:
        data = request.get_json()
        username = data.get('username')
        password = data.get('password')
        
        if not username or not password:
            return jsonify({'success': False, 'message': 'Username and password required'})
        
        connection = get_db_connection()
        if not connection:
            return jsonify({'success': False, 'message': 'Database connection error'})
        
        cursor = connection.cursor()
        cursor.execute("""
            SELECT id, username, password_hash, role, total_score, games_played 
            FROM users WHERE username = %s
        """, (username,))
        
        user = cursor.fetchone()
        cursor.close()
        connection.close()
        
        if user and verify_password(password, user['password_hash']):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            return jsonify({
                'success': True,
                'user': {
                    'id': user['id'],
                    'username': user['username'],
                    'role': user['role'],
                    'total_score': user['total_score'],
                    'games_played': user['games_played']
                }
            })
        else:
            return jsonify({'success': False, 'message': 'Invalid credentials'})
            
    except Exception as e:
        print(f"Login error: {e}")
        return jsonify({'success': False, 'message': 'Server error during login'})

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True, 'message': 'Logged out successfully'})

# Add basic error handlers
@app.errorhandler(404)
def not_found(error):
    return jsonify({'success': False, 'message': 'Endpoint not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({'success': False, 'message': 'Internal server error'}), 500

# SocketIO Events
@socketio.on('connect')
def handle_connect():
    print(f'Client connected: {request.sid}')

@socketio.on('disconnect')
def handle_disconnect():
    print(f'Client disconnected: {request.sid}')

# Run the application
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    print(f"Starting server on port {port}")
    print(f"Database initialized: {database_initialized}")
    
    if os.getenv('FLASK_ENV') == 'production':
        socketio.run(app, host='0.0.0.0', port=port, debug=False)
    else:
        socketio.run(app, debug=True, host='0.0.0.0', port=port)
else:
    # This runs when deployed with Gunicorn
    port = int(os.getenv('PORT', 8080))
    print(f"Application loaded for production on port {port}")
    print(f"Database initialized: {database_initialized}")
