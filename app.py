from flask import Flask, request, jsonify, session
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room
import pymysql
import hashlib
import secrets
from datetime import datetime, timedelta
import json
import os
from functools import wraps

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', secrets.token_hex(16))

# Enable CORS for all domains
CORS(app, supports_credentials=True)

# Initialize SocketIO for real-time features - Fixed async_mode
socketio = SocketIO(app, cors_allowed_origins="*")

# Database configuration
DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'user': os.getenv('DB_USER', 'root'),
    'password': os.getenv('DB_PASSWORD', ''),
    'database': os.getenv('DB_NAME', 'buzzer_quiz_game'),
    'port': int(os.getenv('DB_PORT', 3306))
}
# Global game state
game_state = {
    'active_games': {},
    'connected_users': {},
    'current_question': None,
    'timer_active': False,
    'buzzed_player': None
}

def get_db_connection():
    """Create and return a database connection"""
    try:
        connection = pymysql.connect(
            host=DB_CONFIG['host'],
            user=DB_CONFIG['user'],
            password=DB_CONFIG['password'],
            database=DB_CONFIG.get('database'),
            charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True
        )
        return connection
    except Exception as e:
        print(f"Error connecting to MySQL: {e}")
        return None

def init_database():
    """Initialize the database and create tables"""
    try:
        # First connect without database to create it
        temp_config = DB_CONFIG.copy()
        temp_config.pop('database', None)
        
        connection = pymysql.connect(
            host=temp_config['host'],
            user=temp_config['user'],
            password=temp_config['password'],
            charset='utf8mb4'
        )
        cursor = connection.cursor()
        
        # Create database if it doesn't exist
        cursor.execute("CREATE DATABASE IF NOT EXISTS buzzer_quiz_game")
        cursor.close()
        connection.close()
        
        # Now connect with the database
        connection = get_db_connection()
        if not connection:
            return False
            
        cursor = connection.cursor()
        
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
        
        # Insert default admin user if not exists
        admin_password = hashlib.sha256("admin123".encode()).hexdigest()
        cursor.execute("""
            INSERT IGNORE INTO users (username, password_hash, role) 
            VALUES ('admin', %s, 'admin')
        """, (admin_password,))
        
        cursor.close()
        connection.close()
        
        print("Database initialized successfully!")
        return True
        
    except Exception as e:
        print(f"Error initializing database: {e}")
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
# Add this route to your app.py file, after your imports and before other routes


@app.route('/api/login', methods=['POST'])
def login():
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

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True, 'message': 'Logged out successfully'})

@app.route('/api/register', methods=['POST'])
@admin_required
def register_player():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')
    
    if not username or not password:
        return jsonify({'success': False, 'message': 'Username and password required'})
    
    connection = get_db_connection()
    if not connection:
        return jsonify({'success': False, 'message': 'Database connection error'})
    
    cursor = connection.cursor()
    
    # Check if username already exists
    cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
    if cursor.fetchone():
        cursor.close()
        connection.close()
        return jsonify({'success': False, 'message': 'Username already exists'})
    
    # Insert new user
    password_hash = hash_password(password)
    cursor.execute("""
        INSERT INTO users (username, password_hash, role) 
        VALUES (%s, %s, 'player')
    """, (username, password_hash))
    
    connection.commit()
    cursor.close()
    connection.close()
    
    return jsonify({'success': True, 'message': 'Player registered successfully'})

# Quiz Management Routes
@app.route('/api/quizzes', methods=['GET'])
@login_required
def get_quizzes():
    connection = get_db_connection()
    if not connection:
        return jsonify({'success': False, 'message': 'Database connection error'})
    
    cursor = connection.cursor()
    cursor.execute("""
        SELECT q.id, q.title, q.correct_points, q.wrong_points, q.time_per_question, 
               q.created_at, u.username as created_by,
               COUNT(qs.id) as question_count
        FROM quizzes q
        LEFT JOIN users u ON q.created_by = u.id
        LEFT JOIN questions qs ON q.id = qs.quiz_id
        GROUP BY q.id
        ORDER BY q.created_at DESC
    """)
    
    quizzes = []
    for row in cursor.fetchall():
        quizzes.append({
            'id': row['id'],
            'title': row['title'],
            'correct_points': row['correct_points'],
            'wrong_points': row['wrong_points'],
            'time_per_question': row['time_per_question'],
            'created_at': row['created_at'].isoformat() if row['created_at'] else None,
            'created_by': row['created_by'],
            'question_count': row['question_count'] or 0
        })
    
    cursor.close()
    connection.close()
    
    return jsonify({'success': True, 'quizzes': quizzes})

@app.route('/api/quizzes', methods=['POST'])
@admin_required
def create_quiz():
    data = request.get_json()
    title = data.get('title')
    correct_points = data.get('correct_points', 10)
    wrong_points = data.get('wrong_points', 5)
    time_per_question = data.get('time_per_question', 30)
    questions = data.get('questions', [])
    
    if not title:
        return jsonify({'success': False, 'message': 'Quiz title required'})
    
    connection = get_db_connection()
    if not connection:
        return jsonify({'success': False, 'message': 'Database connection error'})
    
    cursor = connection.cursor()
    
    # Insert quiz
    cursor.execute("""
        INSERT INTO quizzes (title, created_by, correct_points, wrong_points, time_per_question)
        VALUES (%s, %s, %s, %s, %s)
    """, (title, session['user_id'], correct_points, wrong_points, time_per_question))
    
    quiz_id = connection.insert_id()
    
    # Insert questions
    for i, question in enumerate(questions):
        options_json = json.dumps(question.get('options', {})) if question.get('options') else None
        cursor.execute("""
            INSERT INTO questions (quiz_id, question_text, question_type, options, correct_answer, question_order)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (quiz_id, question['text'], question['type'], options_json, question.get('correct_answer'), i))
    
    connection.commit()
    cursor.close()
    connection.close()
    
    return jsonify({'success': True, 'message': 'Quiz created successfully', 'quiz_id': quiz_id})

@app.route('/api/quizzes/<int:quiz_id>', methods=['GET'])
@login_required
def get_quiz(quiz_id):
    connection = get_db_connection()
    if not connection:
        return jsonify({'success': False, 'message': 'Database connection error'})
    
    cursor = connection.cursor()
    
    # Get quiz details
    cursor.execute("""
        SELECT id, title, correct_points, wrong_points, time_per_question, created_at
        FROM quizzes WHERE id = %s
    """, (quiz_id,))
    
    quiz = cursor.fetchone()
    if not quiz:
        cursor.close()
        connection.close()
        return jsonify({'success': False, 'message': 'Quiz not found'})
    
    # Get questions
    cursor.execute("""
        SELECT id, question_text, question_type, options, correct_answer, question_order
        FROM questions WHERE quiz_id = %s ORDER BY question_order
    """, (quiz_id,))
    
    questions = []
    for row in cursor.fetchall():
        questions.append({
            'id': row['id'],
            'text': row['question_text'],
            'type': row['question_type'],
            'options': json.loads(row['options']) if row['options'] else None,
            'correct_answer': row['correct_answer'],
            'order': row['question_order']
        })
    
    cursor.close()
    connection.close()
    
    quiz_data = {
        'id': quiz['id'],
        'title': quiz['title'],
        'settings': {
            'correct_points': quiz['correct_points'],
            'wrong_points': quiz['wrong_points'],
            'time_per_question': quiz['time_per_question']
        },
        'questions': questions,
        'created_at': quiz['created_at'].isoformat() if quiz['created_at'] else None
    }
    
    return jsonify({'success': True, 'quiz': quiz_data})

# Game Session Management
@app.route('/api/sessions', methods=['POST'])
@admin_required
def create_session():
    data = request.get_json()
    quiz_id = data.get('quiz_id')
    
    if not quiz_id:
        return jsonify({'success': False, 'message': 'Quiz ID required'})
    
    connection = get_db_connection()
    if not connection:
        return jsonify({'success': False, 'message': 'Database connection error'})
    
    cursor = connection.cursor()
    
    # Generate unique session code
    session_code = secrets.token_hex(4).upper()
    
    cursor.execute("""
        INSERT INTO game_sessions (quiz_id, host_id, session_code)
        VALUES (%s, %s, %s)
    """, (quiz_id, session['user_id'], session_code))
    
    session_id = connection.insert_id()
    cursor.close()
    connection.close()
    
    return jsonify({
        'success': True, 
        'session_id': session_id,
        'session_code': session_code
    })

@app.route('/api/sessions/<int:session_id>/start', methods=['POST'])
@admin_required
def start_session(session_id):
    connection = get_db_connection()
    if not connection:
        return jsonify({'success': False, 'message': 'Database connection error'})
    
    cursor = connection.cursor()
    cursor.execute("""
        UPDATE game_sessions SET status = 'active' WHERE id = %s AND host_id = %s
    """, (session_id, session['user_id']))
    
    cursor.close()
    connection.close()
    
    # Emit to all connected clients
    socketio.emit('game_started', {'session_id': session_id}, room=f'session_{session_id}')
    
    return jsonify({'success': True, 'message': 'Game session started'})

# Players Management
@app.route('/api/players', methods=['GET'])
@admin_required
def get_players():
    connection = get_db_connection()
    if not connection:
        return jsonify({'success': False, 'message': 'Database connection error'})
    
    cursor = connection.cursor()
    cursor.execute("""
        SELECT id, username, total_score, games_played, created_at
        FROM users WHERE role = 'player'
        ORDER BY total_score DESC
    """)
    
    players = []
    for row in cursor.fetchall():
        players.append({
            'id': row['id'],
            'username': row['username'],
            'total_score': row['total_score'],
            'games_played': row['games_played'],
            'created_at': row['created_at'].isoformat() if row['created_at'] else None
        })
    
    cursor.close()
    connection.close()
    
    return jsonify({'success': True, 'players': players})

@app.route('/api/players/<int:player_id>', methods=['DELETE'])
@admin_required
def delete_player(player_id):
    connection = get_db_connection()
    if not connection:
        return jsonify({'success': False, 'message': 'Database connection error'})
    
    cursor = connection.cursor()
    cursor.execute("DELETE FROM users WHERE id = %s AND role = 'player'", (player_id,))
    
    if cursor.rowcount > 0:
        cursor.close()
        connection.close()
        return jsonify({'success': True, 'message': 'Player deleted successfully'})
    else:
        cursor.close()
        connection.close()
        return jsonify({'success': False, 'message': 'Player not found'})

# SocketIO Events for Real-time Features
@socketio.on('connect')
def handle_connect():
    print(f'Client connected: {request.sid}')

@socketio.on('disconnect')
def handle_disconnect():
    print(f'Client disconnected: {request.sid}')
    # Clean up user from game state
    for session_id, session_data in game_state['active_games'].items():
        if request.sid in session_data.get('connected_players', {}):
            del session_data['connected_players'][request.sid]

@socketio.on('join_session')
def handle_join_session(data):
    session_id = data.get('session_id')
    user_id = data.get('user_id')
    
    if session_id and user_id:
        join_room(f'session_{session_id}')
        
        # Initialize session in game state if not exists
        if session_id not in game_state['active_games']:
            game_state['active_games'][session_id] = {
                'connected_players': {},
                'current_question': 0,
                'buzzed_player': None,
                'timer_active': False
            }
        
        game_state['active_games'][session_id]['connected_players'][request.sid] = {
            'user_id': user_id,
            'connected_at': datetime.now()
        }
        
        emit('joined_session', {'session_id': session_id})

@socketio.on('buzz')
def handle_buzz(data):
    session_id = data.get('session_id')
    user_id = data.get('user_id')
    
    if session_id in game_state['active_games']:
        session_data = game_state['active_games'][session_id]
        
        # Check if no one has buzzed yet
        if not session_data.get('buzzed_player'):
            session_data['buzzed_player'] = {
                'user_id': user_id,
                'buzz_time': datetime.now()
            }
            
            # Notify all players in the session
            socketio.emit('player_buzzed', {
                'user_id': user_id,
                'session_id': session_id
            }, room=f'session_{session_id}')

@socketio.on('submit_answer')
def handle_submit_answer(data):
    session_id = data.get('session_id')
    user_id = data.get('user_id')
    answer = data.get('answer')
    
    # Emit to admin for review
    socketio.emit('answer_submitted', {
        'user_id': user_id,
        'answer': answer,
        'session_id': session_id
    }, room=f'session_{session_id}')

# Health check endpoint
@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'healthy', 'message': 'Server is running'})

# Dashboard stats
@app.route('/api/stats', methods=['GET'])
@admin_required
def get_dashboard_stats():
    connection = get_db_connection()
    if not connection:
        return jsonify({'success': False, 'message': 'Database connection error'})
    
    cursor = connection.cursor()
    
    # Get player count
    cursor.execute("SELECT COUNT(*) as count FROM users WHERE role = 'player'")
    player_count = cursor.fetchone()['count']
    
    # Get quiz count
    cursor.execute("SELECT COUNT(*) as count FROM quizzes")
    quiz_count = cursor.fetchone()['count']
    
    # Get active sessions
    cursor.execute("SELECT COUNT(*) as count FROM game_sessions WHERE status = 'active'")
    active_sessions = cursor.fetchone()['count']
    
    cursor.close()
    connection.close()
    
    return jsonify({
        'success': True,
        'stats': {
            'total_players': player_count,
            'total_quizzes': quiz_count,
            'active_sessions': active_sessions
        }
    })

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    if os.getenv('FLASK_ENV') == 'production':
        socketio.run(app, host='0.0.0.0', port=port, debug=False)
    else:
        socketio.run(app, debug=True, host='0.0.0.0', port=port)


