import json
import math
import os
import re
import secrets
import string
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

from flask import Flask, request, jsonify, send_from_directory

try:
    import fcntl  # Linux / Mac（Renderはここ）。Windowsには無いので、その場合は下のthreading.Lockだけ使う
except ImportError:
    fcntl = None

# static_folder=None：このフォルダのファイルが勝手に公開されないようにする
app = Flask(__name__, static_folder=None)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 部屋データの保存先
DATA_FILE = os.environ.get(
    'NEKO_DATA_FILE',
    os.path.join(tempfile.gettempdir(), 'neko_rooms.json')
)
LOCK_FILE = DATA_FILE + '.lock'
_thread_lock = threading.Lock()

ROOM_ID_CHARS = string.ascii_letters + string.digits
ALLOWED_FISH_LIMITS = (1, 3, 5, 7, 10, 999)
ALLOWED_CAT_LIMITS = (10, 20, 30, 50, 999)
SECRET_CAT_LIMIT_CODE = '0518'


def _now():
    return datetime.now()


def _parse_time(value):
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return _now()


def load_db():
    """ファイルから全部屋を読み込む。48時間以上更新のない部屋は取り除く。"""
    try:
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            db = json.load(f)
    except (FileNotFoundError, ValueError):
        db = {}

    now = _now()
    for room_id in list(db.keys()):
        updated_at = _parse_time(db[room_id].get('updated_at', ''))
        if now - updated_at > timedelta(hours=48):
            del db[room_id]

    return db


def save_db(db):
    """途中で壊れないように、一時ファイルに書いてから入れ替える。"""
    tmp_path = DATA_FILE + '.tmp.' + secrets.token_hex(4)

    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(db, f, ensure_ascii=False)

    os.replace(tmp_path, DATA_FILE)


@contextmanager
def locked_db():
    """読む → 変更する → 書く、を他のワーカーと同時にやらないようにする。"""
    with _thread_lock:
        lock = open(LOCK_FILE, 'a+')

        try:
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_EX)

            db = load_db()
            yield db
            save_db(db)

        finally:
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_UN)

            lock.close()


def generate_room_id(db):
    while True:
        length = secrets.choice(range(20, 31))
        room_id = ''.join(
            secrets.choice(ROOM_ID_CHARS)
            for _ in range(length)
        )

        if room_id not in db:
            return room_id


# ==========================================
# トップページ
# ==========================================

@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'index.html')


# ==========================================
# 猫画像の配信
# neko_iconフォルダ内の画像を表示する
# ==========================================

@app.route('/neko_icon/<path:filename>')
def serve_neko_icon(filename):
    icon_dir = os.path.join(BASE_DIR, 'neko_icon')
    return send_from_directory(icon_dir, filename)


# ==========================================
# 部屋情報の取得
# ==========================================

@app.route('/api/room_info')
def get_room_info():
    room_id = request.args.get('room', '')
    db = load_db()

    if room_id in db:
        info = db[room_id]['info']

        return jsonify({
            'exists': True,
            'name': info['name'],
            'theme': info['theme'],
            'color': info['color'],
            'fish_limit': info.get('fish_limit', 3),
            'max_cats': info.get('max_cats', 20),
            'cat_addition_disabled': bool(
                info.get('cat_addition_disabled', False)
            )
        })

    return jsonify({'exists': False})


# ==========================================
# 管理コードの確認
# ==========================================

@app.route('/api/verify_code')
def verify_code():
    room_id = request.args.get('room', '')
    code = str(request.args.get('code', ''))

    db = load_db()

    if room_id not in db:
        return jsonify({'valid': False}), 404

    valid = secrets.compare_digest(
        str(db[room_id]['info'].get('code', '')),
        code
    )

    return jsonify({'valid': valid})


# ==========================================
# 部屋の作成
# ==========================================

@app.route('/api/create_room', methods=['POST'])
def create_room():
    data = request.get_json(silent=True)

    if not data or not data.get('name') or not data.get('code'):
        return jsonify({
            'status': 'error',
            'message': '入力項目が不足しています'
        }), 400

    if not re.fullmatch(r'\d{4}', str(data.get('code', ''))):
        return jsonify({
            'status': 'error',
            'message': '管理コードは4桁の数字で入力してください'
        }), 400

    try:
        fish_limit = int(data.get('fish_limit', 3))
    except (TypeError, ValueError):
        fish_limit = 3

    if fish_limit not in ALLOWED_FISH_LIMITS:
        fish_limit = 3

    with locked_db() as db:
        room_id = generate_room_id(db)

        db[room_id] = {
            'info': {
                'name': str(data['name'])[:20],
                'theme': data.get('theme', 'みんなの散歩道'),
                'code': str(data['code']),
                'color': data.get('color', 'yellow'),
                'fish_limit': fish_limit,
                'max_cats': 20,
                'cat_addition_disabled': False
            },
            'cats': [],
            'clear_version': 0,
            'free_positions': [],
            'updated_at': _now().isoformat()
        }

    return jsonify({
        'status': 'ok',
        'room_id': room_id
    })


# ==========================================
# 猫データの取得
# ==========================================

@app.route('/data.json')
def get_data_json():
    room_id = request.args.get('room', '')
    db = load_db()

    if room_id in db:
        response = jsonify(db[room_id].get('cats', []))
        response.headers['X-Clear-Version'] = str(
            db[room_id].get('clear_version', 0)
        )
        response.headers['Cache-Control'] = 'no-store'
        return response

    response = jsonify([])
    response.headers['X-Clear-Version'] = '0'
    response.headers['Cache-Control'] = 'no-store'

    return response


# ==========================================
# 猫データの保存
# ==========================================

@app.route('/save', methods=['POST'])
def save_data():
    room_id = request.args.get('room', '')
    data = request.get_json(silent=True)

    if data is None:
        return jsonify({'status': 'error'}), 400

    with locked_db() as db:
        if room_id in db:
            room = db[room_id]
            existing = room.get('cats', [])

            try:
                client_clear_version = int(
                    request.headers.get('X-Clear-Version', '-1')
                )
            except (TypeError, ValueError):
                client_clear_version = -1

            server_clear_version = int(
                room.get('clear_version', 0)
            )

            if client_clear_version != server_clear_version:
                return jsonify({
                    'status': 'stale',
                    'message': '部屋の猫一覧が更新されています',
                    'clear_version': server_clear_version
                }), 409

            if not isinstance(data, list):
                return jsonify({
                    'status': 'error',
                    'message': '猫データの形式が正しくありません'
                }), 400

            old_ids = {
                str(cat.get('id'))
                for cat in existing
                if isinstance(cat, dict) and cat.get('id') is not None
            }

            new_ids = {
                str(cat.get('id'))
                for cat in data
                if isinstance(cat, dict) and cat.get('id') is not None
            }

            if (
                room.get('info', {}).get('cat_addition_disabled', False)
                and not new_ids.issubset(old_ids)
            ):
                return jsonify({
                    'status': 'error',
                    'message': 'この部屋では猫の追加が禁止されています'
                }), 403

            # いなくなった猫の座標を空き場所として保存し、
            # 新しい猫に優先的に再利用する。
            free_positions = room.setdefault('free_positions', [])

            removed = [
                cat for cat in existing
                if isinstance(cat, dict)
                and cat.get('id') is not None
                and str(cat.get('id')) not in new_ids
            ]

            for cat in removed:
                x, y = cat.get('x'), cat.get('y')

                if (
                    isinstance(x, (int, float))
                    and isinstance(y, (int, float))
                    and math.isfinite(x)
                    and math.isfinite(y)
                ):
                    free_positions.append({
                        'x': float(x),
                        'y': float(y)
                    })

            free_positions[:] = free_positions[-200:]

            saved_cats = [
                dict(cat) if isinstance(cat, dict) else cat
                for cat in data
            ]

            for cat in saved_cats:
                if not isinstance(cat, dict):
                    continue

                cat_id = cat.get('id')

                if (
                    cat_id is not None
                    and str(cat_id) not in old_ids
                    and free_positions
                ):
                    position = free_positions.pop(0)
                    cat['x'] = position['x']
                    cat['y'] = position['y']

            room['cats'] = saved_cats
            room['updated_at'] = _now().isoformat()

            return jsonify({
                'status': 'ok',
                'clear_version': server_clear_version,
                'cats': saved_cats
            })

    return jsonify({'status': 'error'}), 400


# ==========================================
# 部屋の管理操作
# ==========================================

@app.route('/api/room_action', methods=['POST'])
def room_action():
    data = request.get_json(silent=True) or {}

    room_id = data.get('room')
    action = data.get('action')
    code = str(data.get('code', ''))

    with locked_db() as db:
        if room_id not in db:
            return jsonify({
                'status': 'error',
                'message': '部屋が存在しません'
            }), 404

        room_info = db[room_id]['info']

        # 通常操作は部屋ごとの管理コードで照合。
        # 猫の最大表示数変更だけは、裏設定コード「0518」でも許可する。
        valid_code = room_info['code'] == code

        if action == 'change_max_cats' and code == SECRET_CAT_LIMIT_CODE:
            valid_code = True

        if not valid_code:
            return jsonify({
                'status': 'error',
                'message': '管理コードが正しくありません'
            }), 403

        # ----------------------------------
        # 部屋の削除
        # ----------------------------------

        if action == 'delete':
            del db[room_id]

            return jsonify({
                'status': 'ok',
                'action': 'delete'
            })

        # ----------------------------------
        # 猫の追加禁止設定
        # ----------------------------------

        elif action == 'change_cat_addition':
            disabled = bool(data.get('disabled', False))

            room_info['cat_addition_disabled'] = disabled
            db[room_id]['updated_at'] = _now().isoformat()

            return jsonify({
                'status': 'ok',
                'action': action,
                'cat_addition_disabled': disabled
            })

        # ----------------------------------
        # 猫を全て削除
        # ----------------------------------

        elif action == 'clear_cats':
            room = db[room_id]
            free_positions = room.setdefault('free_positions', [])

            for cat in room.get('cats', []):
                if isinstance(cat, dict):
                    x, y = cat.get('x'), cat.get('y')

                    if (
                        isinstance(x, (int, float))
                        and isinstance(y, (int, float))
                        and math.isfinite(x)
                        and math.isfinite(y)
                    ):
                        free_positions.append({
                            'x': float(x),
                            'y': float(y)
                        })

            free_positions[:] = free_positions[-200:]

            room['cats'] = []
            room['clear_version'] = int(
                room.get('clear_version', 0)
            ) + 1

            db[room_id]['updated_at'] = _now().isoformat()

            return jsonify({
                'status': 'ok',
                'action': 'clear_cats',
                'clear_version': db[room_id]['clear_version']
            })

        # ----------------------------------
        # 部屋のテーマ変更
        # ----------------------------------

        elif action == 'change_theme':
            new_theme = str(
                data.get('new_theme', '')
            ).strip()[:30]

            room_info['theme'] = new_theme
            db[room_id]['updated_at'] = _now().isoformat()

            return jsonify({
                'status': 'ok',
                'action': 'change_theme',
                'new_theme': new_theme
            })

        # ----------------------------------
        # 猫の最大表示数変更
        # ----------------------------------

        elif action == 'change_max_cats':
            try:
                new_limit = int(data.get('max_cats', 20))
            except (TypeError, ValueError):
                return jsonify({
                    'status': 'error',
                    'message': '上限数の指定が正しくありません'
                }), 400

            if new_limit not in ALLOWED_CAT_LIMITS:
                return jsonify({
                    'status': 'error',
                    'message': '指定できる上限数ではありません'
                }), 400

            room_info['max_cats'] = new_limit
            db[room_id]['updated_at'] = _now().isoformat()

            return jsonify({
                'status': 'ok',
                'action': 'change_max_cats',
                'max_cats': new_limit
            })

    return jsonify({
        'status': 'error',
        'message': '無効な操作です'
    }), 400


# ==========================================
# サーバー起動
# ==========================================

if __name__ == '__main__':
    app.run(
        host='0.0.0.0',
        port=5000,
        debug=True
    )
