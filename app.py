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

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', secrets.token_hex(16))

# Enable CORS for all domains
CORS(app, supports_credentials=True)

# Initialize SocketIO for real-time features
socketio = SocketIO(app, cors_allowed_origins="*")

# Database configuration
DB_CONFIG = {
    'host': os.getenv('MYSQLHOST', 'localhost'),
    'user': os.getenv('MYSQLUSER', 'root'),
    'password': os.getenv('MYSQL_ROOT_PASSWORD', ''),
    'database': os.getenv('MYSQL_DATABASE', 'buzzer_quiz_game'),
    'port': int(os.getenv('MYSQLPORT', '3306'))
}

# Global game state
game_state = {
    'active_games': {},
    'connected_users': {},
    'current_session': None,
    'timer_active': False,
    'buzzed_player': None,
    'current_quiz': None,
    'current_question_index': 0,
    'players_in_session': {}
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
        database_name = temp_config.pop('database', 'buzzer_quiz_game')
        
        connection = pymysql.connect(
            host=temp_config['host'],
            user=temp_config['user'],
            password=temp_config['password'],
            charset='utf8mb4'
        )
        cursor = connection.cursor()
        
        # Create database if it doesn't exist
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS {database_name}")
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

        .buzz-button {
            width: 150px;
            height: 150px;
            border-radius: 50%;
            font-size: 24px;
            font-weight: bold;
            background: linear-gradient(45deg, #FF6B6B, #4ECDC4);
            border: 5px solid white;
            cursor: pointer;
            transition: all 0.3s ease;
            margin: 20px auto;
            display: block;
            animation: pulse 2s infinite;
        }

        @keyframes pulse {
            0% { transform: scale(1); }
            50% { transform: scale(1.05); }
            100% { transform: scale(1); }
        }

        .buzz-button:hover {
            transform: scale(1.1);
            box-shadow: 0 0 30px rgba(255, 107, 107, 0.6);
        }

        .buzz-button:active {
            transform: scale(0.95);
        }

        .buzz-button.disabled {
            background: #ccc;
            cursor: not-allowed;
            animation: none;
        }

        .question-display {
            text-align: center;
            font-size: 1.5em;
            margin: 30px 0;
            padding: 20px;
            background: rgba(255, 255, 255, 0.1);
            border-radius: 15px;
            min-height: 100px;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .timer {
            font-size: 3em;
            text-align: center;
            margin: 20px 0;
            color: #FFD700;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.5);
        }

        .timer.warning {
            color: #FF6B6B;
            animation: flash 1s infinite;
        }

        @keyframes flash {
            0%, 50% { opacity: 1; }
            51%, 100% { opacity: 0.5; }
        }

        .leaderboard {
            background: rgba(255, 255, 255, 0.1);
            border-radius: 15px;
            padding: 20px;
            margin: 20px 0;
        }

        .leaderboard-item {
            display: flex;
            justify-content: space-between;
            padding: 15px;
            margin: 10px 0;
            border-radius: 10px;
            background: rgba(255, 255, 255, 0.1);
            transition: all 0.3s ease;
        }

        .leaderboard-item:hover {
            transform: translateX(5px);
            background: rgba(255, 255, 255, 0.2);
        }

        .winner {
            background: linear-gradient(45deg, #FFD700, #FFA500);
            color: #333;
            font-weight: bold;
            font-size: 1.2em;
            animation: glow 2s infinite alternate;
        }

        @keyframes glow {
            from { box-shadow: 0 0 20px rgba(255, 215, 0, 0.5); }
            to { box-shadow: 0 0 30px rgba(255, 215, 0, 0.8); }
        }

        .player-list, .question-list {
            max-height: 300px;
            overflow-y: auto;
            margin: 20px 0;
        }

        .player-item, .question-item {
            background: rgba(255, 255, 255, 0.1);
            padding: 15px;
            margin: 10px 0;
            border-radius: 10px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .admin-monitor {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin: 20px 0;
        }

        .monitor-section {
            background: rgba(255, 255, 255, 0.1);
            padding: 20px;
            border-radius: 15px;
        }

        .buzz-alert {
            background: linear-gradient(45deg, #FF6B6B, #4ECDC4);
            padding: 15px;
            border-radius: 10px;
            margin: 10px 0;
            text-align: center;
            font-weight: bold;
            animation: buzzer-flash 0.5s ease-in-out;
        }

        @keyframes buzzer-flash {
            0% { transform: scale(1); }
            50% { transform: scale(1.05); background: #FFD700; }
            100% { transform: scale(1); }
        }

        .options-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
            margin: 20px 0;
        }

        .option-btn {
            padding: 15px;
            border: none;
            border-radius: 10px;
            background: rgba(255, 255, 255, 0.1);
            color: white;
            cursor: pointer;
            transition: all 0.3s ease;
            font-size: 16px;
        }

        .option-btn:hover {
            background: rgba(255, 255, 255, 0.2);
            transform: translateY(-2px);
        }

        .option-btn.correct {
            background: linear-gradient(45deg, #56ab2f, #a8e6cf);
        }

        .option-btn.incorrect {
            background: linear-gradient(45deg, #FF416C, #FF4B2B);
        }

        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin: 20px 0;
        }

        .stat-card {
            background: rgba(255, 255, 255, 0.1);
            padding: 20px;
            border-radius: 15px;
            text-align: center;
        }

        .stat-number {
            font-size: 2em;
            font-weight: bold;
            color: #FFD700;
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

        @media (max-width: 768px) {
            .container {
                padding: 10px;
            }
            
            h1 {
                font-size: 2em;
            }
            
            .admin-monitor {
                grid-template-columns: 1fr;
            }
            
            .options-grid {
                grid-template-columns: 1fr;
            }
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
                    <input type="text" id="username" placeholder="Enter username">
                </div>
                <div class="form-group">
                    <label>Password:</label>
                    <input type="password" id="password" placeholder="Enter password">
                </div>
                <button class="btn btn-primary" onclick="login()">Login</button>
            #     <div id="loginMessage"></div>
            #     <div style="margin-top: 20px; padding: 15px; background: rgba(255, 255, 255, 0.1); border-radius: 10px;">
            #         <h3>Default Login Credentials:</h3>
            #         <p><strong>Admin:</strong> admin / admin123</p>
            #         <p><em>Create player accounts from the admin dashboard</em></p>
            #     </div>
            # </div>
        </div>

        <!-- Admin Dashboard -->
        <div id="adminDashboard" class="screen">
            <div class="card">
                <h2>🛠 Admin Dashboard</h2>
                <div class="stats-grid">
                    <div class="stat-card">
                        <div class="stat-number" id="totalPlayers">0</div>
                        <div>Total Players</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-number" id="totalQuizzes">0</div>
                        <div>Total Quizzes</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-number" id="activeSessions">0</div>
                        <div>Active Sessions</div>
                    </div>
                </div>
                <div style="text-align: center; margin-bottom: 20px;">
                    <button class="btn btn-primary" onclick="showCreateQuiz()">Create Quiz</button>
                    <button class="btn btn-success" onclick="showManagePlayers()">Manage Players</button>
                    <button class="btn btn-warning" onclick="showGameMonitor()">Game Monitor</button>
                    <button class="btn btn-danger" onclick="logout()">Logout</button>
                </div>
            </div>
        </div>

        <!-- Create Quiz Screen -->
        <div id="createQuizScreen" class="screen">
            <div class="card">
                <h2>📝 Create Quiz</h2>
                <div class="form-group">
                    <label>Quiz Title:</label>
                    <input type="text" id="quizTitle" placeholder="Enter quiz title">
                </div>
                <div class="form-group">
                    <label>Points for Correct Answer:</label>
                    <input type="number" id="correctPoints" value="10" min="1">
                </div>
                <div class="form-group">
                    <label>Points Deducted for Wrong Answer:</label>
                    <input type="number" id="wrongPoints" value="5" min="0">
                </div>
                <div class="form-group">
                    <label>Time per Question (seconds):</label>
                    <input type="number" id="timePerQuestion" value="30" min="5">
                </div>
                
                <h3>Add Questions</h3>
                <div class="form-group">
                    <label>Question:</label>
                    <textarea id="questionText" placeholder="Enter your question"></textarea>
                </div>
                <div class="form-group">
                    <label>Question Type:</label>
                    <select id="questionType" onchange="toggleAnswerFields()">
                        <option value="mcq">Multiple Choice</option>
                        <option value="open">Open Ended</option>
                    </select>
                </div>
                <div id="mcqOptions">
                    <div class="form-group">
                        <label>Option A:</label>
                        <input type="text" id="optionA" placeholder="Enter option A">
                    </div>
                    <div class="form-group">
                        <label>Option B:</label>
                        <input type="text" id="optionB" placeholder="Enter option B">
                    </div>
                    <div class="form-group">
                        <label>Option C:</label>
                        <input type="text" id="optionC" placeholder="Enter option C">
                    </div>
                    <div class="form-group">
                        <label>Option D:</label>
                        <input type="text" id="optionD" placeholder="Enter option D">
                    </div>
                    <div class="form-group">
                        <label>Correct Answer:</label>
                        <select id="correctAnswer">
                            <option value="A">A</option>
                            <option value="B">B</option>
                            <option value="C">C</option>
                            <option value="D">D</option>
                        </select>
                    </div>
                </div>
                <div id="openAnswer" style="display: none;">
                    <div class="form-group">
                        <label>Correct Answer (optional):</label>
                        <input type="text" id="openCorrectAnswer" placeholder="Enter correct answer (optional)">
                    </div>
                </div>
                
                <button class="btn btn-success" onclick="addQuestion()">Add Question</button>
                <button class="btn btn-primary" onclick="saveQuiz()">Save Quiz</button>
                <button class="btn" onclick="showAdminDashboard()">Back to Dashboard</button>
                
                <div class="question-list" id="questionList"></div>
            </div>
        </div>

        <!-- Manage Players Screen -->
        <div id="managePlayersScreen" class="screen">
            <div class="card">
                <h2>👥 Manage Players</h2>
                <div class="form-group">
                    <label>Player Username:</label>
                    <input type="text" id="playerUsername" placeholder="Enter player username">
                </div>
                <div class="form-group">
                    <label>Player Password:</label>
                    <input type="password" id="playerPassword" placeholder="Enter player password">
                </div>
                <button class="btn btn-success" onclick="addPlayer()">Add Player</button>
                <button class="btn" onclick="showAdminDashboard()">Back to Dashboard</button>
                
                <div class="player-list" id="playerList"></div>
            </div>
        </div>

        <!-- Game Monitor Screen -->
        <div id="gameMonitorScreen" class="screen">
            <div class="card">
                <h2>🖥 Game Monitor</h2>
                <div style="text-align: center; margin-bottom: 20px;">
                    <select id="quizSelector" onchange="selectQuiz()">
                        <option value="">Select a quiz</option>
                    </select>
                    <button class="btn btn-success" onclick="startQuiz()" id="startQuizBtn">Start Quiz</button>
                    <button class="btn btn-danger" onclick="endQuiz()" id="endQuizBtn" style="display: none;">End Quiz</button>
                    <button class="btn btn-warning" onclick="nextQuestion()" id="nextQuestionBtn" style="display: none;">Next Question</button>
                    <button class="btn" onclick="showAdminDashboard()">Back to Dashboard</button>
                </div>
                
                <div class="admin-monitor">
                    <div class="monitor-section">
                        <h3>Current Question</h3>
                        <div id="currentQuestionDisplay">Select a quiz to start</div>
                        <div class="timer" id="adminTimer">--</div>
                    </div>
                    <div class="monitor-section">
                        <h3>Buzz Activity</h3>
                        <div id="buzzActivity">Waiting for players...</div>
                    </div>
                </div>
                
                <div class="monitor-section">
                    <h3>Live Leaderboard</h3>
                    <div id="liveLeaderboard">No players yet</div>
                </div>
                
                <div id="answerControls" style="display: none; text-align: center; margin: 20px 0;">
                    <button class="btn btn-success" onclick="markAnswer(true)">✓ Correct</button>
                    <button class="btn btn-danger" onclick="markAnswer(false)">✗ Wrong</button>
                </div>
            </div>
        </div>

        <!-- Player Screen -->
        <div id="playerScreen" class="screen">
            <div class="card">
                <h2>🎮 Player Dashboard</h2>
                <div id="playerWaiting">
                    <h3>Waiting for quiz to start...</h3>
                    <p>Admin will start the quiz shortly. Get ready!</p>
                    <div class="leaderboard">
                        <h4>Connected Players</h4>
                        <div id="connectedPlayersList">Loading...</div>
                    </div>
                </div>
                
                <div id="playerGame" style="display: none;">
                    <div class="question-display" id="playerQuestion">Question will appear here</div>
                    <div class="timer" id="playerTimer">--</div>
                    
                    <button class="buzz-button" id="buzzButton" onclick="buzz()">
                        🔔 BUZZ!
                    </button>
                    
                    <div id="playerOptions" style="display: none;"></div>
                    <div id="openAnswerInput" style="display: none;">
                        <div class="form-group">
                            <label>Your Answer:</label>
                            <input type="text" id="playerAnswer" placeholder="Type your answer">
                            <button class="btn btn-primary" onclick="submitAnswer()">Submit Answer</button>
                        </div>
                    </div>
                    
                    <div id="playerFeedback" style="margin: 20px 0; text-align: center;"></div>
                    
                    <div class="leaderboard">
                        <h4>Live Leaderboard</h4>
                        <div id="playerLeaderboard">Loading...</div>
                    </div>
                </div>
                
                <div id="gameResults" style="display: none;">
                    <h3>🏆 Game Results</h3>
                    <div id="finalLeaderboard"></div>
                </div>
                
                <button class="btn btn-danger" onclick="logout()" style="margin-top: 20px;">Logout</button>
            </div>
        </div>
    </div>

    <script>
        // Configuration
        const API_BASE_URL = '/api';
        
        // Game State
        let gameState = {
            currentUser: null,
            isAdmin: false,
            quiz: null,
            currentQuestionIndex: 0,
            gameActive: false,
            timer: null,
            timeLeft: 0,
            sessionId: null,
            socket: null,
            buzzed: false,
            connectedPlayers: []
        };

        // Initialize Socket.IO connection
        function initializeSocket() {
            if (gameState.socket) return;
            
            gameState.socket = io();
            
            gameState.socket.on('connect', () => {
                console.log('Connected to server');
                // Join as player immediately after connection
                if (gameState.currentUser) {
                    gameState.socket.emit('join_as_player', {
                        user_id: gameState.currentUser.id,
                        username: gameState.currentUser.username,
                        is_admin: gameState.isAdmin
                    });
                }
            });
            
            gameState.socket.on('disconnect', () => {
                console.log('Disconnected from server');
            });
            
            gameState.socket.on('game_started', (data) => {
                console.log('Game started event received:', data);
                if (!gameState.isAdmin) {
                    gameState.gameActive = true;
                    gameState.quiz = data.quiz;
                    gameState.sessionId = data.session_id;
                    document.getElementById('playerWaiting').style.display = 'none';
                    document.getElementById('playerGame').style.display = 'block';
                    showMessage('playerFeedback', 'Game Started! 🚀', 'success');
                }
            });
            
            gameState.socket.on('question_update', (data) => {
                console.log('Question update received:', data);
                if (!gameState.isAdmin) {
                    gameState.currentQuestionIndex = data.question_index;
                    displayPlayerQuestion(data.question);
                    startPlayerTimer(data.time_limit);
                    resetBuzzButton();
                }
            });
            
            gameState.socket.on('player_buzzed', (data) => {
                console.log('Player buzzed event:', data);
                if (gameState.isAdmin) {
                    updateBuzzActivity(`🔔 ${data.username} buzzed!`);
                } else if (data.user_id !== gameState.currentUser?.id) {
                    // Disable buzz for other players
                    const buzzButton = document.getElementById('buzzButton');
                    if (buzzButton) {
                        buzzButton.disabled = true;
                        buzzButton.classList.add('disabled');
                        buzzButton.textContent = `${data.username} Buzzed!`;
                    }
                }
            });
            
            gameState.socket.on('answer_result', (data) => {
                console.log('Answer result received:', data);
                if (!gameState.isAdmin && data.user_id === gameState.currentUser?.id) {
                    showAnswerFeedback(data.is_correct, data.message || 'Answer submitted');
                }
            });
            
            gameState.socket.on('leaderboard_update', (data) => {
                console.log('Leaderboard update received:', data);
                updateLeaderboard(data.leaderboard);
            });
            
            gameState.socket.on('players_update', (data) => {
                console.log('Players update received:', data);
                gameState.connectedPlayers = data.players;
                updateConnectedPlayersList();
                if (gameState.isAdmin) {
                    updateAdminLeaderboard();
                }
            });
            
            gameState.socket.on('game_ended', (data) => {
                console.log('Game ended event received:', data);
                gameState.gameActive = false;
                if (gameState.timer) clearInterval(gameState.timer);
                
                if (!gameState.isAdmin) {
                    document.getElementById('playerGame').style.display = 'none';
                    document.getElementById('gameResults').style.display = 'block';
                    document.getElementById('finalLeaderboard').innerHTML = generateLeaderboardHTML(data.final_leaderboard);
                }
            });
            
            gameState.socket.on('timer_update', (data) => {
                if (!gameState.isAdmin) {
                    updatePlayerTimer(data.time_left);
                }
            });
        }

        // API Helper Functions
        async function apiRequest(endpoint, method = 'GET', data = null) {
            const options = {
                method,
                headers: {
                    'Content-Type': 'application/json',
                },
                credentials: 'include'
            };
            
            if (data) {
                options.body = JSON.stringify(data);
            }
            
            try {
                const response = await fetch(`${API_BASE_URL}${endpoint}`, options);
                return await response.json();
            } catch (error) {
                console.error('API request failed:', error);
                return { success: false, message: 'Network error' };
            }
        }

        // Authentication
        async function login() {
            const username = document.getElementById('username').value.trim();
            const password = document.getElementById('password').value;
            
            if (!username || !password) {
                showMessage('loginMessage', 'Please enter both username and password', 'error');
                return;
            }
            
            showLoading('loginMessage');
            const result = await apiRequest('/login', 'POST', { username, password });
            
            if (result.success) {
                gameState.currentUser = result.user;
                gameState.isAdmin = result.user.role === 'admin';
                
                initializeSocket();
                
                if (gameState.isAdmin) {
                    showScreen('adminDashboard');
                    loadDashboardStats();
                } else {
                    showScreen('playerScreen');
                    // Load connected players for waiting screen
                    updateConnectedPlayersList();
                }
                
                showMessage('loginMessage', `Welcome ${result.user.username}! 🎮`, 'success');
            } else {
                showMessage('loginMessage', result.message, 'error');
            }
        }

        async function logout() {
            await apiRequest('/logout', 'POST');
            gameState.currentUser = null;
            gameState.isAdmin = false;
            gameState.gameActive = false;
            
            if (gameState.timer) clearInterval(gameState.timer);
            if (gameState.socket) {
                gameState.socket.disconnect();
                gameState.socket = null;
            }
            
            document.getElementById('username').value = '';
            document.getElementById('password').value = '';
            showScreen('loginScreen');
        }

        // Dashboard Functions
        async function loadDashboardStats() {
            const result = await apiRequest('/stats');
            if (result.success) {
                document.getElementById('totalPlayers').textContent = result.stats.total_players;
                document.getElementById('totalQuizzes').textContent = result.stats.total_quizzes;
                document.getElementById('activeSessions').textContent = result.stats.active_sessions;
            }
        }

        // Quiz Management
        let currentQuiz = { questions: [] };

        function toggleAnswerFields() {
            const questionType = document.getElementById('questionType').value;
            const mcqOptions = document.getElementById('mcqOptions');
            const openAnswer = document.getElementById('openAnswer');
            
            if (questionType === 'mcq') {
                mcqOptions.style.display = 'block';
                openAnswer.style.display = 'none';
            } else {
                mcqOptions.style.display = 'none';
                openAnswer.style.display = 'block';
            }
        }

        function addQuestion() {
            const questionText = document.getElementById('questionText').value.trim();
            const questionType = document.getElementById('questionType').value;
            
            if (!questionText) {
                alert('Please enter a question');
                return;
            }
            
            const question = {
                id: Date.now(),
                text: questionText,
                type: questionType
            };
            
            if (questionType === 'mcq') {
                const optionA = document.getElementById('optionA').value.trim();
                const optionB = document.getElementById('optionB').value.trim();
                const optionC = document.getElementById('optionC').value.trim();
                const optionD = document.getElementById('optionD').value.trim();
                const correctAnswer = document.getElementById('correctAnswer').value;
                
                if (!optionA || !optionB || !optionC || !optionD) {
                    alert('Please fill all options');
                    return;
                }
                
                question.options = { A: optionA, B: optionB, C: optionC, D: optionD };
                question.correct_answer = correctAnswer;
                
                // Clear fields
                ['optionA', 'optionB', 'optionC', 'optionD'].forEach(id => {
                    document.getElementById(id).value = '';
                });
            } else {
                question.correct_answer = document.getElementById('openCorrectAnswer').value.trim();
                document.getElementById('openCorrectAnswer').value = '';
            }
            
            currentQuiz.questions.push(question);
            document.getElementById('questionText').value = '';
            displayQuestions();
        }

        function displayQuestions() {
            const questionList = document.getElementById('questionList');
            if (!currentQuiz.questions.length) {
                questionList.innerHTML = '<p>No questions added yet</p>';
                return;
            }
            
            questionList.innerHTML = currentQuiz.questions.map((q, index) => `
                <div class="question-item">
                    <div>
                        <strong>Q${index + 1}:</strong> ${q.text}
                        <br><small>Type: ${q.type.toUpperCase()}</small>
                        ${q.type === 'mcq' ? `<br><small>Answer: ${q.correct_answer}</small>` : ''}
                    </div>
                    <button class="btn btn-danger" onclick="removeQuestion(${q.id})">Remove</button>
                </div>
            `).join('');
        }

        function removeQuestion(questionId) {
            currentQuiz.questions = currentQuiz.questions.filter(q => q.id !== questionId);
            displayQuestions();
        }

        async function saveQuiz() {
            const title = document.getElementById('quizTitle').value.trim();
            const correctPoints = parseInt(document.getElementById('correctPoints').value);
            const wrongPoints = parseInt(document.getElementById('wrongPoints').value);
            const timePerQuestion = parseInt(document.getElementById('timePerQuestion').value);
            
            if (!title) {
                alert('Please enter a quiz title');
                return;
            }
            
            if (!currentQuiz.questions.length) {
                alert('Please add at least one question');
                return;
            }
            
            const quizData = {
                title,
                correct_points: correctPoints,
                wrong_points: wrongPoints,
                time_per_question: timePerQuestion,
                questions: currentQuiz.questions
            };
            
            const result = await apiRequest('/quizzes', 'POST', quizData);
            
            if (result.success) {
                alert('Quiz saved successfully!');
                currentQuiz = { questions: [] };
                displayQuestions();
                showAdminDashboard();
            } else {
                alert('Error saving quiz: ' + result.message);
            }
        }

        // Player Management
        async function addPlayer() {
            const username = document.getElementById('playerUsername').value.trim();
            const password = document.getElementById('playerPassword').value;
            
            if (!username || !password) {
                alert('Please enter both username and password');
                return;
            }
            
            const result = await apiRequest('/register', 'POST', { username, password });
            
            if (result.success) {
                document.getElementById('playerUsername').value = '';
                document.getElementById('playerPassword').value = '';
                loadPlayerList();
                alert('Player added successfully!');
            } else {
                alert('Error: ' + result.message);
            }
        }

        async function loadPlayerList() {
            const result = await apiRequest('/players');
            const playerList = document.getElementById('playerList');
            
            if (result.success && result.players.length > 0) {
                playerList.innerHTML = result.players.map(player => `
                    <div class="player-item">
                        <div>
                            <strong>${player.username}</strong>
                            <br><small>Score: ${player.total_score} | Games: ${player.games_played}</small>
                        </div>
                        <button class="btn btn-danger" onclick="removePlayer(${player.id})">Remove</button>
                    </div>
                `).join('');
            } else {
                playerList.innerHTML = '<p>No players found</p>';
            }
        }

        async function removePlayer(playerId) {
            if (confirm('Are you sure you want to remove this player?')) {
                const result = await apiRequest(`/players/${playerId}`, 'DELETE');
                if (result.success) {
                    loadPlayerList();
                    alert('Player removed successfully');
                } else {
                    alert('Error removing player: ' + result.message);
                }
            }
        }

        // Game Monitor Functions
        async function loadQuizzes() {
            const result = await apiRequest('/quizzes');
            const quizSelector = document.getElementById('quizSelector');
            
            if (result.success) {
                quizSelector.innerHTML = '<option value="">Select a quiz</option>' + 
                    result.quizzes.map(quiz => 
                        `<option value="${quiz.id}">${quiz.title} (${quiz.question_count} questions)</option>`
                    ).join('');
            }
        }

        async function selectQuiz() {
            const quizId = document.getElementById('quizSelector').value;
            if (!quizId) return;
            
            const result = await apiRequest(`/quizzes/${quizId}`);
            if (result.success) {
                gameState.quiz = result.quiz;
                document.getElementById('currentQuestionDisplay').textContent = 'Quiz loaded: ' + result.quiz.title;
            }
        }

        async function startQuiz() {
            if (!gameState.quiz) {
                alert('Please select a quiz first');
                return;
            }
            
            // Create game session
            const sessionResult = await apiRequest('/sessions', 'POST', { quiz_id: gameState.quiz.id });
            if (!sessionResult.success) {
                alert('Error creating session: ' + sessionResult.message);
                return;
            }
            
            gameState.sessionId = sessionResult.session_id;
            
            // Start the session
            const startResult = await apiRequest(`/sessions/${gameState.sessionId}/start`, 'POST');
            if (startResult.success) {
                gameState.gameActive = true;
                gameState.currentQuestionIndex = 0;
                
                document.getElementById('startQuizBtn').style.display = 'none';
                document.getElementById('endQuizBtn').style.display = 'inline';
                document.getElementById('nextQuestionBtn').style.display = 'inline';
                
                // Start first question
                displayCurrentQuestion();
                startAdminTimer();
                updateBuzzActivity('Quiz started! Waiting for players...');
            }
        }

        function endQuiz() {
            gameState.gameActive = false;
            if (gameState.timer) clearInterval(gameState.timer);
            
            // Emit game ended
            if (gameState.socket) {
                gameState.socket.emit('end_game', {
                    session_id: gameState.sessionId
                });
            }
            
            document.getElementById('startQuizBtn').style.display = 'inline';
            document.getElementById('endQuizBtn').style.display = 'none';
            document.getElementById('nextQuestionBtn').style.display = 'none';
            document.getElementById('answerControls').style.display = 'none';
            
            alert('Quiz ended!');
        }

        function nextQuestion() {
            if (gameState.currentQuestionIndex < gameState.quiz.questions.length - 1) {
                gameState.currentQuestionIndex++;
                document.getElementById('answerControls').style.display = 'none';
                displayCurrentQuestion();
                startAdminTimer();
                
                // Emit next question to players
                if (gameState.socket) {
                    const question = gameState.quiz.questions[gameState.currentQuestionIndex];
                    gameState.socket.emit('next_question', {
                        session_id: gameState.sessionId,
                        question: question,
                        question_index: gameState.currentQuestionIndex,
                        time_limit: gameState.quiz.settings.time_per_question
                    });
                }
            } else {
                endQuiz();
            }
        }

        function displayCurrentQuestion() {
            if (!gameState.quiz || !gameState.gameActive) return;
            
            const question = gameState.quiz.questions[gameState.currentQuestionIndex];
            const questionDisplay = `Q${gameState.currentQuestionIndex + 1}: ${question.text}`;
            
            document.getElementById('currentQuestionDisplay').textContent = questionDisplay;
            
            // Emit question to all players
            if (gameState.socket && gameState.currentQuestionIndex === 0) {
                // For first question, emit game_started
                gameState.socket.emit('start_game', {
                    session_id: gameState.sessionId,
                    quiz: gameState.quiz,
                    question: question,
                    question_index: gameState.currentQuestionIndex,
                    time_limit: gameState.quiz.settings.time_per_question
                });
            }
        }

        function startAdminTimer() {
            if (gameState.timer) clearInterval(gameState.timer);
            
            gameState.timeLeft = gameState.quiz.settings.time_per_question;
            updateAdminTimerDisplay();
            
            gameState.timer = setInterval(() => {
                gameState.timeLeft--;
                updateAdminTimerDisplay();
                
                // Emit timer update to players
                if (gameState.socket) {
                    gameState.socket.emit('timer_update', {
                        session_id: gameState.sessionId,
                        time_left: gameState.timeLeft
                    });
                }
                
                if (gameState.timeLeft <= 0) {
                    clearInterval(gameState.timer);
                    handleAdminTimeUp();
                }
            }, 1000);
        }

        function updateAdminTimerDisplay() {
            const timer = document.getElementById('adminTimer');
            if (timer) {
                const timeText = gameState.timeLeft > 0 ? gameState.timeLeft : 'TIME UP!';
                timer.textContent = timeText;
                timer.classList.toggle('warning', gameState.timeLeft <= 10);
            }
        }

        function handleAdminTimeUp() {
            updateBuzzActivity('⏰ Time up! Moving to next question.');
            setTimeout(() => nextQuestion(), 2000);
        }

        // Player Functions
        function displayPlayerQuestion(question) {
            const playerQuestion = document.getElementById('playerQuestion');
            if (!playerQuestion) return;
            
            const questionDisplay = `Q${gameState.currentQuestionIndex + 1}: ${question.text}`;
            playerQuestion.textContent = questionDisplay;
            
            const playerOptions = document.getElementById('playerOptions');
            const openAnswerInput = document.getElementById('openAnswerInput');
            
            if (question.type === 'mcq') {
                playerOptions.style.display = 'block';
                openAnswerInput.style.display = 'none';
                playerOptions.innerHTML = Object.entries(question.options).map(([key, value]) => 
                    `<button class="option-btn" onclick="selectOption('${key}')" id="option${key}">${key}. ${value}</button>`
                ).join('');
            } else {
                playerOptions.style.display = 'none';
                openAnswerInput.style.display = 'block';
                document.getElementById('playerAnswer').value = '';
            }
        }

        function startPlayerTimer(timeLimit) {
            gameState.timeLeft = timeLimit;
            updatePlayerTimer(timeLimit);
        }

        function updatePlayerTimer(timeLeft) {
            const timer = document.getElementById('playerTimer');
            if (timer) {
                const timeText = timeLeft > 0 ? timeLeft : 'TIME UP!';
                timer.textContent = timeText;
                timer.classList.toggle('warning', timeLeft <= 10);
            }
        }

        function buzz() {
            if (!gameState.gameActive || gameState.buzzed) return;
            
            gameState.buzzed = true;
            
            // Disable buzz button
            const buzzButton = document.getElementById('buzzButton');
            buzzButton.disabled = true;
            buzzButton.classList.add('disabled');
            buzzButton.textContent = 'BUZZED!';
            
            // Emit buzz to server
            if (gameState.socket) {
                gameState.socket.emit('player_buzz', {
                    session_id: gameState.sessionId,
                    user_id: gameState.currentUser.id,
                    username: gameState.currentUser.username
                });
            }
            
            // Show answer options
            const question = gameState.quiz.questions[gameState.currentQuestionIndex];
            if (question.type === 'mcq') {
                document.getElementById('playerOptions').style.display = 'block';
            } else {
                document.getElementById('openAnswerInput').style.display = 'block';
            }
        }

        function selectOption(option) {
            const question = gameState.quiz.questions[gameState.currentQuestionIndex];
            const isCorrect = option === question.correct_answer;
            
            // Highlight selected option
            document.querySelectorAll('.option-btn').forEach(btn => {
                btn.disabled = true;
                if (btn.id === `option${option}`) {
                    btn.classList.add(isCorrect ? 'correct' : 'incorrect');
                }
                if (btn.id === `option${question.correct_answer}`) {
                    btn.classList.add('correct');
                }
            });
            
            // Emit answer to server
            if (gameState.socket) {
                gameState.socket.emit('submit_answer', {
                    session_id: gameState.sessionId,
                    user_id: gameState.currentUser.id,
                    answer: option,
                    is_correct: isCorrect,
                    question_index: gameState.currentQuestionIndex
                });
            }
        }

        function submitAnswer() {
            const playerAnswer = document.getElementById('playerAnswer').value.trim();
            if (!playerAnswer) {
                alert('Please enter an answer');
                return;
            }
            
            // Disable input
            document.getElementById('playerAnswer').disabled = true;
            
            // Emit answer to server for admin review
            if (gameState.socket) {
                gameState.socket.emit('submit_answer', {
                    session_id: gameState.sessionId,
                    user_id: gameState.currentUser.id,
                    answer: playerAnswer,
                    question_index: gameState.currentQuestionIndex
                });
            }
            
            showAnswerFeedback(null, 'Admin is reviewing your answer...');
        }

        function showAnswerFeedback(isCorrect, message) {
            const playerFeedback = document.getElementById('playerFeedback');
            if (!playerFeedback) return;
            
            if (isCorrect === null) {
                playerFeedback.innerHTML = `<div class="message" style="background: #ffa500; padding: 15px; border-radius: 10px;">⏳ ${message}</div>`;
            } else if (isCorrect) {
                playerFeedback.innerHTML = `<div class="message success" style="padding: 15px; border-radius: 10px;">✅ Correct! Well done!</div>`;
            } else {
                playerFeedback.innerHTML = `<div class="message error" style="padding: 15px; border-radius: 10px;">❌ Wrong! ${message}</div>`;
            }
        }

        function resetBuzzButton() {
            gameState.buzzed = false;
            const buzzButton = document.getElementById('buzzButton');
            if (buzzButton) {
                buzzButton.disabled = false;
                buzzButton.classList.remove('disabled');
                buzzButton.textContent = '🔔 BUZZ!';
            }
            
            // Hide answer options
            document.getElementById('playerOptions').style.display = 'none';
            document.getElementById('openAnswerInput').style.display = 'none';
            document.getElementById('playerAnswer').disabled = false;
            
            // Clear feedback
            document.getElementById('playerFeedback').innerHTML = '';
        }

        // Admin Functions
        function markAnswer(isCorrect) {
            updateBuzzActivity(`Admin marked answer as ${isCorrect ? 'CORRECT' : 'WRONG'}`);
            
            // Emit result to player
            if (gameState.socket) {
                gameState.socket.emit('answer_result', {
                    session_id: gameState.sessionId,
                    is_correct: isCorrect,
                    message: isCorrect ? 'Correct!' : 'Wrong answer'
                });
            }
            
            document.getElementById('answerControls').style.display = 'none';
            setTimeout(() => nextQuestion(), 2000);
        }

        function updateBuzzActivity(message) {
            const buzzActivity = document.getElementById('buzzActivity');
            if (buzzActivity) {
                const alertDiv = document.createElement('div');
                alertDiv.className = 'buzz-alert';
                alertDiv.textContent = message;
                buzzActivity.insertBefore(alertDiv, buzzActivity.firstChild);
                
                // Keep only last 5 messages
                while (buzzActivity.children.length > 5) {
                    buzzActivity.removeChild(buzzActivity.lastChild);
                }
            }
        }

        // Leaderboard Functions
        function updateLeaderboard(leaderboard) {
            // Update player leaderboard
            const playerLeaderboard = document.getElementById('playerLeaderboard');
            if (playerLeaderboard) {
                playerLeaderboard.innerHTML = generateLeaderboardHTML(leaderboard);
            }
            
            // Update admin leaderboard
            const liveLeaderboard = document.getElementById('liveLeaderboard');
            if (liveLeaderboard) {
                liveLeaderboard.innerHTML = generateLeaderboardHTML(leaderboard);
            }
        }

        function updateAdminLeaderboard() {
            const liveLeaderboard = document.getElementById('liveLeaderboard');
            if (liveLeaderboard && gameState.connectedPlayers.length > 0) {
                // Sort players by score
                const sortedPlayers = [...gameState.connectedPlayers].sort((a, b) => (b.score || 0) - (a.score || 0));
                liveLeaderboard.innerHTML = generateLeaderboardHTML(sortedPlayers);
            }
        }

        function generateLeaderboardHTML(leaderboard) {
            if (!leaderboard || leaderboard.length === 0) {
                return '<p>No players yet</p>';
            }
            
            return leaderboard.map((player, index) => `
                <div class="leaderboard-item ${index === 0 ? 'winner' : ''}">
                    <span>${index + 1}. ${player.username || player.name}</span>
                    <span>${player.score || player.current_score || 0} points</span>
                </div>
            `).join('');
        }

        function updateConnectedPlayersList() {
            const connectedList = document.getElementById('connectedPlayersList');
            if (connectedList) {
                if (gameState.connectedPlayers.length === 0) {
                    connectedList.innerHTML = '<p>No players connected yet</p>';
                } else {
                    connectedList.innerHTML = gameState.connectedPlayers.map(player => `
                        <div class="leaderboard-item">
                            <span>${player.username}</span>
                            <span>Ready</span>
                        </div>
                    `).join('');
                }
            }
        }

        // UI Helper Functions
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
                    element.innerHTML = '';
                }, 3000);
            }
        }

        function showLoading(elementId) {
            const element = document.getElementById(elementId);
            if (element) {
                element.innerHTML = `
                    <div class="loading">
                        <div class="spinner"></div>
                        <p>Loading...</p>
                    </div>
                `;
            }
        }

        // Navigation Functions
        function showAdminDashboard() {
            showScreen('adminDashboard');
            loadDashboardStats();
        }

        function showCreateQuiz() {
            showScreen('createQuizScreen');
            currentQuiz = { questions: [] };
            displayQuestions();
        }

        function showManagePlayers() {
            showScreen('managePlayersScreen');
            loadPlayerList();
        }

        function showGameMonitor() {
            showScreen('gameMonitorScreen');
            loadQuizzes();
        }

        // Initialize the app
        window.onload = function() {
            console.log('Quiz Game initialized');
        };
    </script>
</body>
</html>'''

# Updated route to serve the HTML page at root
@app.route('/')
def home():
    return render_template_string(get_html_template())

# API Routes
@app.route('/api/info')
def api_info():
    return jsonify({
        'message': 'Buzzer Quiz Game API',
        'status': 'running',
        'version': '1.0.0'
    })

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

# Continuing from where the code was cut off...

@app.route('/api/register', methods=['POST'])
@admin_required
def register():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')
    role = data.get('role', 'player')
    
    if not username or not password:
        return jsonify({'success': False, 'message': 'Username and password required'})
    
    connection = get_db_connection()
    if not connection:
        return jsonify({'success': False, 'message': 'Database connection error'})
    
    try:
        cursor = connection.cursor()
        
        # Check if username already exists
        cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
        if cursor.fetchone():
            return jsonify({'success': False, 'message': 'Username already exists'})
        
        # Create new user
        password_hash = hash_password(password)
        cursor.execute("""
            INSERT INTO users (username, password_hash, role) 
            VALUES (%s, %s, %s)
        """, (username, password_hash, role))
        
        user_id = cursor.lastrowid
        cursor.close()
        connection.close()
        
        return jsonify({
            'success': True, 
            'message': 'User created successfully',
            'user_id': user_id
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error creating user: {str(e)}'})

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
    
    players = cursor.fetchall()
    cursor.close()
    connection.close()
    
    return jsonify({'success': True, 'players': players})

@app.route('/api/players/<int:player_id>', methods=['DELETE'])
@admin_required
def remove_player(player_id):
    connection = get_db_connection()
    if not connection:
        return jsonify({'success': False, 'message': 'Database connection error'})
    
    try:
        cursor = connection.cursor()
        cursor.execute("DELETE FROM users WHERE id = %s AND role = 'player'", (player_id,))
        
        if cursor.rowcount == 0:
            return jsonify({'success': False, 'message': 'Player not found'})
        
        cursor.close()
        connection.close()
        
        return jsonify({'success': True, 'message': 'Player removed successfully'})
        
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error removing player: {str(e)}'})

@app.route('/api/quizzes', methods=['GET'])
@login_required
def get_quizzes():
    connection = get_db_connection()
    if not connection:
        return jsonify({'success': False, 'message': 'Database connection error'})
    
    cursor = connection.cursor()
    cursor.execute("""
        SELECT q.id, q.title, q.correct_points, q.wrong_points, 
               q.time_per_question, q.created_at, u.username as created_by,
               COUNT(qs.id) as question_count
        FROM quizzes q
        LEFT JOIN users u ON q.created_by = u.id
        LEFT JOIN questions qs ON q.id = qs.quiz_id
        GROUP BY q.id
        ORDER BY q.created_at DESC
    """)
    
    quizzes = cursor.fetchall()
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
    
    if not title or not questions:
        return jsonify({'success': False, 'message': 'Title and questions required'})
    
    connection = get_db_connection()
    if not connection:
        return jsonify({'success': False, 'message': 'Database connection error'})
    
    try:
        cursor = connection.cursor()
        
        # Create quiz
        cursor.execute("""
            INSERT INTO quizzes (title, created_by, correct_points, wrong_points, time_per_question)
            VALUES (%s, %s, %s, %s, %s)
        """, (title, session['user_id'], correct_points, wrong_points, time_per_question))
        
        quiz_id = cursor.lastrowid
        
        # Add questions
        for i, question in enumerate(questions):
            options_json = json.dumps(question.get('options')) if question.get('options') else None
            cursor.execute("""
                INSERT INTO questions (quiz_id, question_text, question_type, options, correct_answer, question_order)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (quiz_id, question['text'], question['type'], options_json, 
                  question.get('correct_answer'), i + 1))
        
        cursor.close()
        connection.close()
        
        return jsonify({'success': True, 'quiz_id': quiz_id, 'message': 'Quiz created successfully'})
        
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error creating quiz: {str(e)}'})

@app.route('/api/quizzes/<int:quiz_id>', methods=['GET'])
@login_required
def get_quiz(quiz_id):
    connection = get_db_connection()
    if not connection:
        return jsonify({'success': False, 'message': 'Database connection error'})
    
    cursor = connection.cursor()
    
    # Get quiz details
    cursor.execute("""
        SELECT q.*, u.username as created_by_name
        FROM quizzes q
        LEFT JOIN users u ON q.created_by = u.id
        WHERE q.id = %s
    """, (quiz_id,))
    
    quiz = cursor.fetchone()
    if not quiz:
        return jsonify({'success': False, 'message': 'Quiz not found'})
    
    # Get questions
    cursor.execute("""
        SELECT id, question_text as text, question_type as type, options, correct_answer, question_order
        FROM questions
        WHERE quiz_id = %s
        ORDER BY question_order
    """, (quiz_id,))
    
    questions = cursor.fetchall()
    
    # Parse JSON options
    for question in questions:
        if question['options']:
            question['options'] = json.loads(question['options'])
    
    quiz['questions'] = questions
    quiz['settings'] = {
        'correct_points': quiz['correct_points'],
        'wrong_points': quiz['wrong_points'],
        'time_per_question': quiz['time_per_question']
    }
    
    cursor.close()
    connection.close()
    
    return jsonify({'success': True, 'quiz': quiz})

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
    
    try:
        cursor = connection.cursor()
        
        # Generate unique session code
        session_code = secrets.token_urlsafe(6).upper()
        
        cursor.execute("""
            INSERT INTO game_sessions (quiz_id, host_id, session_code, status)
            VALUES (%s, %s, %s, 'waiting')
        """, (quiz_id, session['user_id'], session_code))
        
        session_id = cursor.lastrowid
        
        # Update global state
        game_state['current_session'] = session_id
        game_state['active_games'][session_id] = {
            'quiz_id': quiz_id,
            'host_id': session['user_id'],
            'session_code': session_code,
            'status': 'waiting',
            'players': {},
            'current_question': 0
        }
        
        cursor.close()
        connection.close()
        
        return jsonify({
            'success': True, 
            'session_id': session_id,
            'session_code': session_code
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error creating session: {str(e)}'})

@app.route('/api/sessions/<int:session_id>/start', methods=['POST'])
@admin_required
def start_session(session_id):
    connection = get_db_connection()
    if not connection:
        return jsonify({'success': False, 'message': 'Database connection error'})
    
    try:
        cursor = connection.cursor()
        cursor.execute("""
            UPDATE game_sessions 
            SET status = 'active' 
            WHERE id = %s AND host_id = %s
        """, (session_id, session['user_id']))
        
        if cursor.rowcount == 0:
            return jsonify({'success': False, 'message': 'Session not found or access denied'})
        
        # Update game state
        if session_id in game_state['active_games']:
            game_state['active_games'][session_id]['status'] = 'active'
        
        cursor.close()
        connection.close()
        
        return jsonify({'success': True, 'message': 'Session started'})
        
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error starting session: {str(e)}'})

@app.route('/api/stats', methods=['GET'])
@admin_required
def get_stats():
    connection = get_db_connection()
    if not connection:
        return jsonify({'success': False, 'message': 'Database connection error'})
    
    cursor = connection.cursor()
    
    # Get total players
    cursor.execute("SELECT COUNT(*) as count FROM users WHERE role = 'player'")
    total_players = cursor.fetchone()['count']
    
    # Get total quizzes
    cursor.execute("SELECT COUNT(*) as count FROM quizzes")
    total_quizzes = cursor.fetchone()['count']
    
    # Get active sessions
    cursor.execute("SELECT COUNT(*) as count FROM game_sessions WHERE status = 'active'")
    active_sessions = cursor.fetchone()['count']
    
    cursor.close()
    connection.close()
    
    return jsonify({
        'success': True,
        'stats': {
            'total_players': total_players,
            'total_quizzes': total_quizzes,
            'active_sessions': active_sessions
        }
    })

# Socket.IO Events
@socketio.on('connect')
def handle_connect():
    print(f'User connected: {request.sid}')

@socketio.on('disconnect')
def handle_disconnect():
    print(f'User disconnected: {request.sid}')
    
    # Remove user from connected users and game state
    if request.sid in game_state['connected_users']:
        user_info = game_state['connected_users'][request.sid]
        del game_state['connected_users'][request.sid]
        
        # Notify other users about disconnection
        emit('players_update', {
            'players': list(game_state['connected_users'].values())
        }, broadcast=True)

@socketio.on('join_as_player')
def handle_join_as_player(data):
    user_id = data.get('user_id')
    username = data.get('username')
    is_admin = data.get('is_admin', False)
    
    # Store user connection info
    game_state['connected_users'][request.sid] = {
        'user_id': user_id,
        'username': username,
        'is_admin': is_admin,
        'score': 0,
        'sid': request.sid
    }
    
    print(f'Player joined: {username} (Admin: {is_admin})')
    
    # Broadcast updated player list to all clients
    players_list = [user for user in game_state['connected_users'].values() if not user['is_admin']]
    emit('players_update', {
        'players': players_list
    }, broadcast=True)

@socketio.on('start_game')
def handle_start_game(data):
    session_id = data.get('session_id')
    quiz = data.get('quiz')
    question = data.get('question')
    question_index = data.get('question_index')
    time_limit = data.get('time_limit')
    
    # Reset game state
    game_state['current_session'] = session_id
    game_state['current_quiz'] = quiz
    game_state['current_question_index'] = question_index
    game_state['buzzed_player'] = None
    
    # Emit to all players
    emit('game_started', {
        'session_id': session_id,
        'quiz': quiz
    }, broadcast=True)
    
    # Emit first question
    emit('question_update', {
        'question': question,
        'question_index': question_index,
        'time_limit': time_limit
    }, broadcast=True)

@socketio.on('next_question')
def handle_next_question(data):
    session_id = data.get('session_id')
    question = data.get('question')
    question_index = data.get('question_index')
    time_limit = data.get('time_limit')
    
    game_state['current_question_index'] = question_index
    game_state['buzzed_player'] = None
    
    emit('question_update', {
        'question': question,
        'question_index': question_index,
        'time_limit': time_limit
    }, broadcast=True)

@socketio.on('player_buzz')
def handle_player_buzz(data):
    session_id = data.get('session_id')
    user_id = data.get('user_id')
    username = data.get('username')
    
    # Only allow first buzz
    if game_state['buzzed_player'] is None:
        game_state['buzzed_player'] = {
            'user_id': user_id,
            'username': username,
            'time': datetime.now()
        }
        
        # Broadcast buzz event
        emit('player_buzzed', {
            'user_id': user_id,
            'username': username,
            'session_id': session_id
        }, broadcast=True)
        
        # Log buzz in database
        connection = get_db_connection()
        if connection:
            try:
                cursor = connection.cursor()
                cursor.execute("""
                    INSERT INTO buzz_log (session_id, user_id, question_id, buzz_time)
                    VALUES (%s, %s, %s, %s)
                """, (session_id, user_id, 
                      game_state['current_quiz']['questions'][game_state['current_question_index']]['id'] if game_state.get('current_quiz') else None,
                      datetime.now()))
                cursor.close()
                connection.close()
            except Exception as e:
                print(f"Error logging buzz: {e}")

@socketio.on('submit_answer')
def handle_submit_answer(data):
    session_id = data.get('session_id')
    user_id = data.get('user_id')
    answer = data.get('answer')
    is_correct = data.get('is_correct')
    question_index = data.get('question_index')
    
    # For MCQ questions, we can immediately determine correctness
    if is_correct is not None:
        points = 0
        if is_correct:
            points = game_state['current_quiz']['settings']['correct_points']
        else:
            points = -game_state['current_quiz']['settings']['wrong_points']
        
        # Update player score
        if request.sid in game_state['connected_users']:
            game_state['connected_users'][request.sid]['score'] = game_state['connected_users'][request.sid].get('score', 0) + points
        
        # Update database
        connection = get_db_connection()
        if connection:
            try:
                cursor = connection.cursor()
                # Update or insert participant score
                cursor.execute("""
                    INSERT INTO game_participants (session_id, user_id, current_score)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE current_score = current_score + %s
                """, (session_id, user_id, points, points))
                
                # Update buzz log
                cursor.execute("""
                    UPDATE buzz_log 
                    SET was_correct = %s, points_awarded = %s
                    WHERE session_id = %s AND user_id = %s 
                    ORDER BY buzz_time DESC LIMIT 1
                """, (is_correct, points, session_id, user_id))
                
                cursor.close()
                connection.close()
            except Exception as e:
                print(f"Error updating scores: {e}")
        
        # Emit result
        emit('answer_result', {
            'user_id': user_id,
            'is_correct': is_correct,
            'points': points,
            'message': 'Correct!' if is_correct else 'Wrong answer'
        }, broadcast=True)
        
        # Update leaderboard
        players_list = [user for user in game_state['connected_users'].values() if not user['is_admin']]
        sorted_players = sorted(players_list, key=lambda x: x.get('score', 0), reverse=True)
        emit('leaderboard_update', {
            'leaderboard': sorted_players
        }, broadcast=True)
    else:
        # For open-ended questions, admin needs to review
        emit('admin_review_needed', {
            'user_id': user_id,
            'username': game_state['connected_users'][request.sid]['username'],
            'answer': answer,
            'session_id': session_id
        }, room=request.sid)

@socketio.on('answer_result')
def handle_answer_result(data):
    session_id = data.get('session_id')
    is_correct = data.get('is_correct')
    message = data.get('message')
    
    emit('answer_result', {
        'session_id': session_id,
        'is_correct': is_correct,
        'message': message
    }, broadcast=True)

@socketio.on('timer_update')
def handle_timer_update(data):
    session_id = data.get('session_id')
    time_left = data.get('time_left')
    
    emit('timer_update', {
        'session_id': session_id,
        'time_left': time_left
    }, broadcast=True)

@socketio.on('end_game')
def handle_end_game(data):
    session_id = data.get('session_id')
    
    # Get final leaderboard
    players_list = [user for user in game_state['connected_users'].values() if not user['is_admin']]
    final_leaderboard = sorted(players_list, key=lambda x: x.get('score', 0), reverse=True)
    
    # Update database
    connection = get_db_connection()
    if connection:
        try:
            cursor = connection.cursor()
            cursor.execute("""
                UPDATE game_sessions 
                SET status = 'completed' 
                WHERE id = %s
            """, (session_id,))
            
            # Update player statistics
            for player in final_leaderboard:
                cursor.execute("""
                    UPDATE users 
                    SET total_score = total_score + %s, games_played = games_played + 1
                    WHERE id = %s
                """, (player.get('score', 0), player['user_id']))
            
            cursor.close()
            connection.close()
        except Exception as e:
            print(f"Error updating final scores: {e}")
    
    # Reset game state
    game_state['current_session'] = None
    game_state['current_quiz'] = None
    game_state['current_question_index'] = 0
    game_state['buzzed_player'] = None
    
    emit('game_ended', {
        'session_id': session_id,
        'final_leaderboard': final_leaderboard
    }, broadcast=True)

# Error handlers
@app.errorhandler(404)
def not_found(error):
    return jsonify({'success': False, 'message': 'Endpoint not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({'success': False, 'message': 'Internal server error'}), 500

# Initialize database and start the application
if __name__ == '__main__':
    print("🎯 Starting Ultimate Buzzer Quiz Game...")
    
    # Initialize database
    if init_database():
        print("✅ Database initialized successfully")
    else:
        print("❌ Database initialization failed")
        exit(1)
    
    # Start the Flask-SocketIO server
    print("🚀 Starting server...")
    print("🌐 Access the game at: http://localhost:5000")
    print("👑 Default admin login: admin / admin123")
    
    socketio.run(
        app, 
        debug=True, 
        host='0.0.0.0', 
        port=5000,
        allow_unsafe_werkzeug=True
    )

