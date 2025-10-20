import os
import threading
import time
from flask import Flask, send_from_directory, request
from flask_socketio import SocketIO, emit
import chess

app = Flask(__name__, static_folder='.')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev')
socketio = SocketIO(
    app, 
    cors_allowed_origins='*', 
    async_mode='threading',
    ping_timeout=5,  # Faster disconnect detection (default is 60)
    ping_interval=2   # Check connection every 2 seconds (default is 25)
)

# Single game state (one room). Extendable to rooms if needed.
board = chess.Board()
players = {'w': None, 'b': None}  # sid of socket
player_ids = {'w': None, 'b': None}  # persistent player_id for each color
sid_to_player_id = {}  # sid -> player_id mapping
names = {}  # sid -> display name
connected_sids = set()  # all currently connected client sids
white_time = 120
black_time = 120
inc_per_move = 0
_timer_thread = None
_timer_lock = threading.Lock()
_timer_running = False
pending_start = None  # {'from': sid, 'base': mins, 'inc': inc, 'opponent': sid or None}
_pending_lock = threading.Lock()

# No reconnection grace period - immediate game over on disconnect


def get_turn():
    return 'w' if board.turn == chess.WHITE else 'b'


def broadcast_state(extra=None):
    fen = board.fen()
    payload = {
        'fen': fen,
        'turn': get_turn(),
        'whiteTime': white_time,
        'blackTime': black_time,
        'inc': inc_per_move,
        'over': board.is_game_over(),
        'checkmate': board.is_checkmate(),
        'draw': (board.is_game_over() and not board.is_checkmate()),
        'whiteName': names.get(players['w'], 'White'),
        'blackName': names.get(players['b'], 'Black'),
    }
    if extra:
        payload.update(extra)
    socketio.emit('state', payload)


def emit_assign_to_sid(sid, started=False):
    """Emit current assignment to a specific sid."""
    color = 'spectator'
    if players['w'] == sid:
        color = 'w'
    elif players['b'] == sid:
        color = 'b'
    emit('assign', {
        'color': color,
        'fen': board.fen(),
        'whiteTime': white_time,
        'blackTime': black_time,
        'inc': inc_per_move,
        'turn': get_turn(),
        'whiteName': names.get(players['w'], 'White'),
        'blackName': names.get(players['b'], 'Black'),
        'started': bool(started),
    }, to=sid)


def broadcast_assignments(started=False):
    """Emit assignment payloads to all connected clients."""
    for sid in list(connected_sids):
        try:
            emit_assign_to_sid(sid, started=started)
        except Exception:
            # best-effort; if a sid dropped, it will be cleaned up on disconnect
            pass


def _timer_loop():
    global white_time, black_time, _timer_running
    while _timer_running:
        time.sleep(1)
        if board.is_game_over():
            _timer_running = False
            break
        with _timer_lock:
            if get_turn() == 'w':
                white_time = max(0, white_time - 1)
                if white_time == 0:
                    socketio.emit('overlay', {'message': 'Black wins on time!'})
                    _timer_running = False
            else:
                black_time = max(0, black_time - 1)
                if black_time == 0:
                    socketio.emit('overlay', {'message': 'White wins on time!'})
                    _timer_running = False
        broadcast_state()


def start_timer():
    global _timer_thread, _timer_running
    if _timer_running:
        return
    _timer_running = True
    _timer_thread = threading.Thread(target=_timer_loop, daemon=True)
    _timer_thread.start()


def stop_timer():
    global _timer_running
    _timer_running = False


def reset_game(base_mins=2, inc=0):
    global board, white_time, black_time, inc_per_move, player_ids
    stop_timer()
    board = chess.Board()
    white_time = int(base_mins) * 60
    black_time = int(base_mins) * 60
    inc_per_move = int(inc)
    # Keep player_ids so players maintain their colors across games
    broadcast_state({'reset': True})


@app.route('/')
def root():
    return send_from_directory('.', 'index.html')


@socketio.on('connect')
def on_connect():
    sid = request.sid
    connected_sids.add(sid)
    print(f"[server] connect: sid={sid}")
    
    # Initial assignment will happen after 'identify' event
    # For now, assign as spectator
    emit_assign_to_sid(sid, started=_timer_running)
    
    # If a game offer is currently pending, make sure late joiners see it
    if pending_start and pending_start.get('from') != sid:
        try:
            socketio.emit(
                'startOffer',
                {
                    'fromName': names.get(pending_start.get('from'), 'Player'),
                    'baseMins': pending_start.get('base', 2),
                    'inc': pending_start.get('inc', 0),
                },
                to=sid,
            )
        except Exception:
            pass


@socketio.on('identify')
def on_identify(data):
    """Handle player identification."""
    sid = request.sid
    player_id = data.get('playerId')
    
    if not player_id:
        return
    
    sid_to_player_id[sid] = player_id
    print(f"[server] identify: sid={sid}, player_id={player_id}")
    
    # Check if this player_id already has a color assigned (only if game not running)
    restored_color = None
    if not _timer_running:
        for color in ['w', 'b']:
            if player_ids[color] == player_id:
                # Same player rejoining - restore their color only if game not active
                players[color] = sid
                restored_color = color
                print(f"[server] {sid} (player_id={player_id}) restored as {color}")
                break
    
    # Assign color if needed
    color = 'spectator'
    if restored_color:
        color = restored_color
    elif players['w'] is None:
        players['w'] = sid
        player_ids['w'] = player_id
        color = 'w'
    elif players['b'] is None:
        players['b'] = sid
        player_ids['b'] = player_id
        color = 'b'
    elif players['w'] == sid:
        color = 'w'
    elif players['b'] == sid:
        color = 'b'
    
    # Send updated assignment
    emit_assign_to_sid(sid, started=_timer_running)
    
    if not restored_color:
        print(f"[server] {sid} assigned as {color}")
    broadcast_state({'note': f'{sid} joined as {color}'})


def request_sid():
    # flask-socketio provides request.sid
    return request.sid


@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    connected_sids.discard(sid)
    
    # Determine if this was a player
    disconnected_color = None
    if players['w'] == sid:
        disconnected_color = 'w'
    elif players['b'] == sid:
        disconnected_color = 'b'
    
    # If a player disconnected during an active game, immediately end it
    if disconnected_color and _timer_running:
        player_name = names.get(sid, 'White' if disconnected_color == 'w' else 'Black')
        print(f"[server] Player {disconnected_color} ({sid}) disconnected during game. Ending game immediately.")
        
        # Stop the timer
        stop_timer()
        
        # Declare the other player as winner
        winner_color = 'b' if disconnected_color == 'w' else 'w'
        winner_sid = players[winner_color]
        winner_name = names.get(winner_sid, 'White' if winner_color == 'w' else 'Black')
        
        socketio.emit('overlay', {
            'message': f'{winner_name} wins - opponent disconnected',
            'winnerName': winner_name,
            'winnerColor': winner_color,
            'reason': 'Opponent disconnected'
        })
    
    # Clear the player slot
    if players['w'] == sid:
        players['w'] = None
        player_ids['w'] = None
    if players['b'] == sid:
        players['b'] = None
        player_ids['b'] = None
    names.pop(sid, None)
    
    global pending_start
    if pending_start and pending_start.get('from') == sid:
        pending_start = None
    
    broadcast_state()


@socketio.on('setTimeControl')
def on_set_time_control(data):
    base = int(data.get('baseMins', 2))
    inc = int(data.get('inc', 0))
    reset_game(base, inc)


@socketio.on('start')
def on_start():
    # Legacy: keep for compatibility but gate actual start behind handshake
    # Only start if no pending handshake and both players present
    if players['w'] and players['b'] and pending_start is None:
        start_timer()
        broadcast_state({'started': True})


@socketio.on('move')
def on_move(data):
    global white_time, black_time
    sid = request.sid
    turn = get_turn()
    # Enforce color per socket id
    if (turn == 'w' and players['w'] != sid) or (turn == 'b' and players['b'] != sid):
        emit('errorMsg', {'message': 'Not your turn'})
        return

    from_sq = data.get('from')
    to_sq = data.get('to')
    promo = (data.get('promotion') or 'q').lower()

    try:
        move = chess.Move.from_uci(
            f"{from_sq}{to_sq}{promo if promo in 'qrbn' and chess.Move.from_uci(f'{from_sq}{to_sq}').promotion is not None else ''}"
        )
    except Exception:
        emit('errorMsg', {'message': 'Illegal move'})
        return

    if move not in board.legal_moves:
        emit('errorMsg', {'message': 'Illegal move'})
        return

    board.push(move)

    # increment for mover
    if turn == 'w':
        white_time += inc_per_move
    else:
        black_time += inc_per_move

    broadcast_state({'lastMove': {'from': from_sq, 'to': to_sq}})

    if board.is_checkmate():
        stop_timer()
        # After board.push(move), get_turn() returns next to move (loser)
        loser_turn = get_turn()  # 'w' or 'b'
        winner_color = 'b' if loser_turn == 'w' else 'w'
        winner_sid = players[winner_color]
        winner_name = names.get(winner_sid, 'White' if winner_color == 'w' else 'Black')
        socketio.emit('overlay', {
            'message': f'{winner_name} wins by checkmate',
            'winnerName': winner_name,
            'winnerColor': winner_color,
            'reason': 'By checkmate'
        })
    elif (board.is_game_over() and not board.is_checkmate()) or \
         board.is_stalemate() or board.is_insufficient_material() or \
         board.is_fivefold_repetition() or board.is_seventyfive_moves() or \
         board.can_claim_fifty_moves() or board.can_claim_threefold_repetition():
        stop_timer()
        socketio.emit('overlay', {'message': 'Draw!'})
    else:
        start_timer()


@socketio.on('setName')
def on_set_name(data):
    name = (data.get('name') or '').strip()
    if not name:
        return
    names[request.sid] = name[:24]
    broadcast_state()


def _other_player_sid(sid):
    if players['w'] == sid:
        return players['b']
    if players['b'] == sid:
        return players['w']
    return None


@socketio.on('proposeStart')
def on_propose_start(data):
    global pending_start, white_time, black_time, inc_per_move
    sid = request.sid
    base = int(data.get('baseMins', 2))
    inc = int(data.get('inc', 0))
    # Anyone in the lobby may propose a start as long as a game isn't running
    # and there isn't already a pending offer.
    if _timer_running:
        print(f"[server] proposeStart ignored: game running (sid={sid})")
        emit('startStatus', {'status': 'game_running'})
        return
    with _pending_lock:
        if pending_start is not None:
            print(f"[server] proposeStart ignored: already pending (sid={sid})")
            emit('startStatus', {'status': 'waiting'})
            return
    # create pending request
    with _pending_lock:
        pending_start = {'from': sid, 'base': base, 'inc': inc, 'opponent': None}
    print(f"[server] proposeStart: from={sid} base={base} inc={inc}")
    emit('startStatus', {'status': 'waiting'})
    # broadcast offer to everyone else in the lobby (all connected except proposer)
    offer_payload = {
        'fromName': names.get(sid, 'Player'),
        'baseMins': base,
        'inc': inc
    }
    for other_sid in list(connected_sids):
        if other_sid == sid:
            continue
        socketio.emit('startOffer', offer_payload, to=other_sid)
    print(f"[server] startOffer broadcast to {len(connected_sids)-1} clients")


@socketio.on('cancelStart')
def on_cancel_start():
    global pending_start
    sid = request.sid
    if pending_start and pending_start.get('from') == sid:
        pending_start = None
        # notify everyone that the offer was cancelled
        for other_sid in list(connected_sids):
            socketio.emit('startStatus', {'status': 'cancelled'}, to=other_sid)


@socketio.on('respondStart')
def on_respond_start(data):
    global pending_start
    sid = request.sid
    accept = bool(data.get('accept'))
    with _pending_lock:
        ps = pending_start
    if not ps:
        print(f"[server] respondStart: no pending offer (sid={sid}, accept={accept})")
        emit('startStatus', {'status': 'no_pending'})
        return
    proposer = ps.get('from')
    if not accept:
        # Inform proposer who rejected
        print(f"[server] respondStart: REJECT sid={sid} -> notify proposer={proposer}")
        socketio.emit('startStatus', {'status': 'rejected', 'byName': names.get(sid, 'Opponent')}, to=proposer)
        emit('startStatus', {'status': 'rejected'})
        return

    # Accept path: take the first accepter only
    with _pending_lock:
        if pending_start and pending_start.get('opponent') is None:
            pending_start['opponent'] = sid
            took_slot = True
            base = pending_start.get('base', 2)
            inc = pending_start.get('inc', 0)
        else:
            took_slot = False

    if took_slot:
        print(f"[server] respondStart: ACCEPT sid={sid} proposer={proposer}")
        # Seat the proposer and opponent into w/b
        # Keep the proposer's current color; opponent gets the other color
        if players['w'] == proposer:
            opponent_color = 'b'
            players['b'] = sid
        elif players['b'] == proposer:
            opponent_color = 'w'
            players['w'] = sid
        else:
            # Proposer somehow lost seat; default proposer to white, accepter to black
            players['w'] = proposer
            opponent_color = 'b'
            players['b'] = sid

        # Ensure both players have default display names if missing
        if not names.get(proposer):
            names[proposer] = 'ChessPlayer'
        if not names.get(sid):
            names[sid] = 'ChessPlayer'

        # Immediately notify proposer and opponent of their roles before starting
        emit_assign_to_sid(proposer, started=False)
        emit_assign_to_sid(sid, started=False)
        # Also update everyone else (spectators) of current assignments
        broadcast_assignments(started=False)

        # Start the game with requested tc
        print(f"[server] starting game base={base} inc={inc}")
        reset_game(base, inc)
        with _pending_lock:
            pending_start = None
        # Acknowledge acceptance to proposer and opponent
        socketio.emit('startStatus', {'status': 'accepted'}, to=proposer)
        socketio.emit('startStatus', {'status': 'accepted'}, to=sid)
        # Start timer and broadcast started state
        start_timer()
        broadcast_state({'started': True})
        # After game starts, ensure everyone (including players) receives started flag
        broadcast_assignments(started=True)
    else:
        print(f"[server] respondStart: SLOTS_FULL sid={sid}")
        # Slots are full; inform this late accepter
        emit('startStatus', {'status': 'slots_full'})


@socketio.on('forfeit')
def on_forfeit():
    sid = request.sid
    # Determine winner
    stop_timer()
    if players['w'] == sid or players['b'] == sid:
        winner_color = 'b' if players['w'] == sid else 'w'
        winner_sid = players[winner_color]
        winner_name = names.get(winner_sid, 'White' if winner_color == 'w' else 'Black')
        socketio.emit('overlay', {
            'message': f'{winner_name} wins by forfeit',
            'winnerName': winner_name,
            'winnerColor': winner_color,
            'reason': 'By forfeit'
        })
    else:
        emit('errorMsg', {'message': 'Spectators cannot forfeit'})
    broadcast_state({'over': True})


@socketio.on('hardReset')
def on_hard_reset():
    """TESTING ONLY: Fully reset server state to lobby without disconnecting clients.
    Resets board, timers, pending offers, and broadcasts clean assignments/state.
    """
    global pending_start
    stop_timer()
    pending_start = None
    reset_game(base_mins=2, inc=0)
    # Ensure clients see non-started lobby state and correct roles
    broadcast_assignments(started=False)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8080'))
    # Use socketio.run to serve Socket.IO endpoint; serve static index.html
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)
