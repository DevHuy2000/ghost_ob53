import os
import sys
import time
import json
import re
import socket
import base64
import binascii
import threading
import pickle
import random
import queue
import urllib3
import asyncio
from datetime import datetime
from threading import Thread
import requests
import psutil
import jwt
from flask import Flask, request, jsonify
from flask_cors import CORS
from concurrent.futures import ThreadPoolExecutor
from google.protobuf.timestamp_pb2 import Timestamp
from protobuf_decoder.protobuf_decoder import Parser
import xKEys
from byte import xSendTeamMsg, Auth_Chat
from byte import *

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─────────────────────────────────────────────
#  GLOBALS
# ─────────────────────────────────────────────

connected_clients: dict = {}
connected_clients_lock = threading.Lock()

# Hàng đợi trung tâm: Flask ném task vào đây, Worker lấy ra xử lý
task_queue: queue.Queue = queue.Queue()

app = Flask(__name__)
CORS(app)

API_KEY = "senzu_new"


# ─────────────────────────────────────────────
#  FLASK API  (chỉ nhận lệnh → enqueue → trả về ngay)
# ─────────────────────────────────────────────

def validate_api_key(api_key: str) -> bool:
    return api_key == API_KEY


@app.route('/')
def home():
    return jsonify({
        "status": "success",
        "message": "buy source: @S_ZU_01",
        "endpoints": {"/send?tc=&name=&api_key="}
    })


@app.route('/send')
def ghost_endpoint():
    teamcode = request.args.get('tc')
    name     = request.args.get('name')
    api_key  = request.args.get('api_key')

    if not api_key or not validate_api_key(api_key):
        return jsonify({"status": "error", "message": "Thiếu Key Rồi.!"}), 401

    if not teamcode or not name:
        return jsonify({"status": "error", "message": "Thiếu Dữ Liệu!"}), 400

    # ✅ Không gọi bot trực tiếp — chỉ ném task vào Queue rồi trả về ngay
    task = {
        "action":   "ghost",
        "teamcode": teamcode,
        "name":     name,
        "ts":       time.time(),
    }
    task_queue.put(task)

    return jsonify({
        "status":  "queued",
        "message": "✅ Success Sending...",
        "task":    task,
    })


def run_flask_api():
    print("[API] Flask đang chạy trên :6002")
    app.run(host='0.0.0.0', port=6002, debug=False)


# ─────────────────────────────────────────────
#  WORKER  (thread riêng, poll queue liên tục)
# ─────────────────────────────────────────────

class TaskWorker(threading.Thread):
    """Thread độc lập, lấy task từ queue và thực thi an toàn."""

    def __init__(self):
        super().__init__(daemon=True, name="TaskWorker")

    def run(self):
        print("[Worker] Đã khởi động, đang chờ task...")
        while True:
            try:
                task = task_queue.get(timeout=1)   # chờ tối đa 1s rồi loop lại
            except queue.Empty:
                continue

            try:
                if task.get("action") == "ghost":
                    self._handle_ghost(task["teamcode"], task["name"])
            except Exception as e:
                print(f"[Worker] Lỗi khi xử lý task {task}: {e}")
            finally:
                task_queue.task_done()

    # ── Ghost logic (tách riêng khỏi Flask) ──────────────────────────────

    def _handle_ghost(self, teamcode: str, name: str):
        print(f"[Worker] Xử lý ghost → teamcode={teamcode}, name={name}")

        if not ChEck_Commande(teamcode):
            print("[Worker] TeamCode không hợp lệ ⚠️")
            return

        with connected_clients_lock:
            if not connected_clients:
                print("[Worker] Không có account nào online ❌")
                return

            clients_list = list(connected_clients.values())

        if len(clients_list) < 3:
            print(f"[Worker] Cần ít nhất 3 account, hiện có {len(clients_list)} ⚠️")
            return

        # Lấy team data qua master client
        master = clients_list[0]
        team_result = self._get_team_data(master, teamcode)

        if not team_result["success"]:
            print(f"[Worker] Không lấy được team data: {team_result.get('message')}")
            return

        team_id   = team_result["team_id"]
        sq_value  = team_result["sq"]
        owner_uid = team_result["owner_uid"]
        print(f"[Worker] Team data OK → ID={team_id}, SQ={sq_value}, Owner UID={owner_uid}")

        # Gửi ghost qua 3 client đầu, dùng thread riêng mỗi client
        ghost_clients = clients_list[:3]
        threads = []
        for i, client in enumerate(ghost_clients, 1):
            t = threading.Thread(
                target=self._execute_ghost,
                args=(client, team_id, owner_uid, name, sq_value, i),
                daemon=True,
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=10)

        print("[Worker] ✅ Hoàn thành ghost!")

    def _get_team_data(self, client, teamcode: str) -> dict:
        try:
            if not (hasattr(client, 'CliEnts2') and client.CliEnts2
                    and hasattr(client, 'key') and client.key
                    and hasattr(client, 'iv')  and client.iv):
                return {"success": False, "message": "Client chưa kết nối đúng."}

            join_packet = JoinTeamCode(teamcode, client.key, client.iv)
            client.CliEnts2.send(join_packet)

            deadline = time.time() + 8
            while time.time() < deadline:
                try:
                    if hasattr(client, 'DaTa2') and client.DaTa2 and len(client.DaTa2.hex()) > 30:
                        hex_data = client.DaTa2.hex()
                        if '0500' in hex_data[:4]:
                            try:
                                raw = f'08{hex_data.split("08", 1)[1]}' if '08' in hex_data else hex_data[10:]
                                dT = json.loads(DeCode_PackEt(raw))
                                if '5' in dT and 'data' in dT['5']:
                                    td = dT['5']['data']
                                    if '31' in td and 'data' in td['31']:
                                        sq  = td['31']['data']
                                        idT = td['1']['data']
                                        # team_id (key '1') chính là UID chủ đội
                                        owner_uid = idT
                                        client.CliEnts2.send(ExitBot('000000', client.key, client.iv))
                                        time.sleep(0.2)
                                        return {"success": True, "team_id": idT, "sq": sq, "owner_uid": owner_uid}
                            except Exception:
                                pass
                except Exception:
                    pass
                time.sleep(0.1)

            return {"success": False, "message": "Timeout chờ phản hồi."}

        except Exception as e:
            return {"success": False, "message": str(e)}

    def _execute_ghost(self, client, team_id, owner_uid, name: str, sq_value, client_number: int):
        try:
            if not (hasattr(client, 'CliEnts2') and client.CliEnts2
                    and hasattr(client, 'key') and client.key
                    and hasattr(client, 'iv')  and client.iv):
                print(f"[Worker] Client #{client_number} chưa sẵn sàng.")
                return

            key, iv = client.key, client.iv

            # Gửi req_join bằng uid chủ đội trước
            rj_packet = req_join(int(owner_uid), key, iv)
            client.CliEnts2.send(rj_packet)
            time.sleep(0.15)

            # Sau đó gửi GhostPakcet
            ghost_packet = GhostPakcet(team_id, name, sq_value, key, iv)
            client.CliEnts2.send(ghost_packet)
            time.sleep(0.5)

            print(f"[Worker] Client #{client_number} ({client.id}) → req_join + ghost OK ✅")

        except Exception as e:
            print(f"[Worker] Client #{client_number} lỗi: {e}")


# ─────────────────────────────────────────────
#  FF CLIENT  (giữ nguyên logic kết nối game)
# ─────────────────────────────────────────────

def generate_random_color():
    color_list = [
        "[00FF00][b][c]", "[FFDD00][b][c]", "[3813F3][b][c]", "[FF0000][b][c]",
        "[0000FF][b][c]", "[FFA500][b][c]", "[DF07F8][b][c]", "[11EAFD][b][c]",
        "[DCE775][b][c]", "[A8E6CF][b][c]", "[7CB342][b][c]", "[FF0000][b][c]",
        "[FFB300][b][c]", "[90EE90][b][c]", "[FF4500][b][c]", "[FFD700][b][c]",
        "[32CD32][b][c]", "[87CEEB][b][c]", "[9370DB][b][c]", "[FF69B4][b][c]",
        "[8A2BE2][b][c]", "[00BFFF][b][c]", "[1E90FF][b][c]", "[20B2AA][b][c]",
        "[00FA9A][b][c]", "[008000][b][c]", "[FFFF00][b][c]", "[FF8C00][b][c]",
        "[DC143C][b][c]", "[FF6347][b][c]",
    ]
    return random.choice(color_list)


def AuTo_ResTartinG():
    time.sleep(6 * 60 * 60)
    print('تمت اعادة تشغيل البوت بنجاح !')
    p = psutil.Process(os.getpid())
    for handler in p.open_files():
        try:
            os.close(handler.fd)
        except Exception as e:
            print(f" - Error CLose Files : {e}")
    for conn in p.net_connections():
        try:
            if hasattr(conn, 'fd'):
                os.close(conn.fd)
        except Exception as e:
            print(f" - Error CLose Connection : {e}")
    sys.path.append(os.path.dirname(os.path.abspath(sys.argv[0])))
    python = sys.executable
    os.execl(python, python, *sys.argv)


def ResTarT_BoT():
    print(' - تم ايجاد خطا سيتم اصلاحه ')
    p = psutil.Process(os.getpid())
    for handler in p.open_files():
        try:
            os.close(handler.fd)
        except Exception:
            pass
    for conn in p.net_connections():
        try:
            conn.close()
        except Exception:
            pass
    sys.path.append(os.path.dirname(os.path.abspath(sys.argv[0])))
    python = sys.executable
    os.execl(python, python, *sys.argv)


def GeT_Time(timestamp):
    last_login = datetime.fromtimestamp(timestamp)
    now  = datetime.now()
    diff = now - last_login
    d    = diff.days
    h, rem = divmod(diff.seconds, 3600)
    m, s   = divmod(rem, 60)
    return d, h, m, s


Thread(target=AuTo_ResTartinG, daemon=True).start()


# ─── Account loader ───────────────────────────

ACCOUNTS = []

def load_accounts_from_file(filename="accs.txt"):
    accounts = []
    try:
        with open(filename, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and ":" in line:
                    parts = line.split(":")
                    if len(parts) >= 2:
                        accounts.append({'id': parts[0].strip(), 'password': parts[1].strip()})
        print(f"Đã tải {len(accounts)} account từ {filename}")
    except FileNotFoundError:
        print(f"Không tìm thấy file {filename}!")
    except Exception as e:
        print(f"Lỗi đọc file: {e}")
    return accounts


ACCOUNTS = load_accounts_from_file()
if not ACCOUNTS:
    ACCOUNTS = [{'id': '4739589262', 'password': 'Senzu_9997DLRF'}]


# ─── FF Client class ──────────────────────────

class FF_CLient:

    def __init__(self, id, password):
        self.id       = id
        self.password = password
        self.DaTa2    = None
        self.Get_FiNal_ToKen_0115()

    # ── Secondary server (data socket) ───────────

    def Connect_SerVer_OnLine(self, Token, tok, host, port, key, iv, host2, port2):
        # Mỗi lần gọi lại, tăng session_id để loop cũ tự thoát yên lặng
        current_session = getattr(self, '_online_session', 0) + 1
        self._online_session = current_session

        try:
            self.AutH_ToKen_0115 = tok
            self.CliEnts2 = socket.create_connection((host2, int(port2)))
            self.CliEnts2.send(bytes.fromhex(self.AutH_ToKen_0115))
        except Exception as e:
            print(f"[{self.id}] Lỗi kết nối server phụ: {e}")
            return

        while self._online_session == current_session:
            try:
                data = self.CliEnts2.recv(99999)
                if not data:
                    print(f"[{self.id}] Server phụ đóng kết nối.")
                    break
                self.DaTa2 = data
                hex_data = data.hex()
                if '0500' in hex_data[:4] and len(hex_data) > 30:
                    try:
                        packet = json.loads(DeCode_PackEt(f'08{hex_data.split("08", 1)[1]}'))
                        if '5' in packet and 'data' in packet['5']:
                            inner = packet['5']['data']
                            if '7' in inner and 'data' in inner['7']:
                                self.AutH = inner['7']['data']
                    except Exception:
                        pass  # packet loại khác, bỏ qua yên lặng
            except OSError:
                if self._online_session != current_session:
                    break  # session cũ bị đóng → thoát yên lặng
                print(f"[{self.id}] Socket phụ bị đóng, thoát loop.")
                break
            except Exception as e:
                if self._online_session != current_session:
                    break
                print(f"[{self.id}] Lỗi nhận data phụ: {e}")
                time.sleep(1)

    # ── Primary server (main socket) ─────────────

    def Connect_SerVer(self, Token, tok, host, port, key, iv, host2, port2):
        self.AutH_ToKen_0115 = tok
        try:
            self.CliEnts = socket.create_connection((host, int(port)))
            self.CliEnts.send(bytes.fromhex(self.AutH_ToKen_0115))
            self.DaTa = self.CliEnts.recv(1024)
        except Exception as e:
            print(f"[{self.id}] Lỗi kết nối server chính: {e}")
            time.sleep(5)
            self.Connect_SerVer(Token, tok, host, port, key, iv, host2, port2)
            return

        threading.Thread(
            target=self.Connect_SerVer_OnLine,
            args=(Token, tok, host, port, key, iv, host2, port2),
            daemon=True,
        ).start()

        self.Exemple = xMsGFixinG('12345678')
        self.key = key
        self.iv  = iv

        with connected_clients_lock:
            connected_clients[self.id] = self
            print(f"[{self.id}] Đã đăng ký, tổng online: {len(connected_clients)}")

        while True:
            try:
                self.DaTa = self.CliEnts.recv(1024)

                if len(self.DaTa) == 0:
                    try:
                        self.CliEnts.close()
                        if hasattr(self, 'CliEnts2'):
                            self.CliEnts2.close()
                        self.Connect_SerVer(Token, tok, host, port, key, iv, host2, port2)
                    except Exception:
                        try:
                            self.CliEnts.close()
                            if hasattr(self, 'CliEnts2'):
                                self.CliEnts2.close()
                            self.Connect_SerVer(Token, tok, host, port, key, iv, host2, port2)
                        except Exception:
                            self.CliEnts.close()
                            if hasattr(self, 'CliEnts2'):
                                self.CliEnts2.close()
                            ResTarT_BoT()

                if '1200' in self.DaTa.hex()[:4] and 900 > len(self.DaTa.hex()) > 100:
                    if b"***" in self.DaTa:
                        self.DaTa = self.DaTa.replace(b"***", b"106")
                    try:
                        self.BesTo_data = json.loads(DeCode_PackEt(self.DaTa.hex()[10:]))
                        self.input_msg = ('besto_love'
                                         if '8' in self.BesTo_data["5"]["data"]
                                         else self.BesTo_data["5"]["data"]["4"]["data"])
                    except Exception:
                        self.input_msg = None

                    self.DeCode_CliEnt_Uid = self.BesTo_data["5"]["data"]["1"]["data"]
                    self.CliEnt_Uid = EnC_Uid(self.DeCode_CliEnt_Uid, Tp='Uid')

                if self.input_msg and 'hi' in self.input_msg[:10]:
                    self.CliEnts.send(
                        GenResponsMsg(f'@S_ZU_01', 2,
                                      self.DeCode_CliEnt_Uid,
                                      self.DeCode_CliEnt_Uid, key, iv)
                    )
                    time.sleep(0.3)
                    self.CliEnts.close()
                    if hasattr(self, 'CliEnts2'):
                        self.CliEnts2.close()
                    self.Connect_SerVer(Token, tok, host, port, key, iv, host2, port2)

                if self.input_msg and (b'@help ' in self.DaTa or b'@help' in self.DaTa
                                       or 'en' in self.input_msg[:2]):
                    self.result = ChEck_The_Uid(self.DeCode_CliEnt_Uid)
                    if self.result:
                        self.Status, self.Expire = self.result
                        self.CliEnts.send(
                            GenResponsMsg(f'@S_ZU_01', 2,
                                          self.DeCode_CliEnt_Uid,
                                          self.DeCode_CliEnt_Uid, key, iv)
                        )

            except Exception as e:
                print(f"[{self.id}] Lỗi vòng lặp chính: {e}")
                try:
                    self.CliEnts.close()
                    if hasattr(self, 'CliEnts2'):
                        self.CliEnts2.close()
                except Exception:
                    pass
                self.Connect_SerVer(Token, tok, host, port, key, iv, host2, port2)

    # ── Auth helpers ──────────────────────────────

    def GeT_Key_Iv(self, serialized_data):
        my_message = xKEys.MyMessage()
        my_message.ParseFromString(serialized_data)
        timestamp, key, iv = my_message.field21, my_message.field22, my_message.field23
        timestamp_obj = Timestamp()
        timestamp_obj.FromNanoseconds(timestamp)
        combined = timestamp_obj.seconds * 1_000_000_000 + timestamp_obj.nanos
        return combined, key, iv

    def Guest_GeneRaTe(self, uid, password):
        url     = "https://100067.connect.garena.com/oauth/guest/token/grant"
        headers = {
            "Host": "100067.connect.garena.com",
            "User-Agent": "GarenaMSDK/4.0.19P4(G011A ;Android 9;en;US;)",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "close",
        }
        data = {
            "uid": uid, "password": password,
            "response_type": "token", "client_type": "2",
            "client_secret": "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3",
            "client_id": "100067",
        }
        try:
            resp = requests.post(url, headers=headers, data=data).json()
            access_token = resp['access_token']
            access_uid   = resp['open_id']
            time.sleep(0.2)
            print(f'[{uid}] Khởi động account...')
            return self.ToKen_GeneRaTe(access_token, access_uid)
        except Exception as e:
            print(f"[{uid}] Lỗi gen token: {e}")
            ResTarT_BoT()

    def GeT_LoGin_PorTs(self, JwT_ToKen, PayLoad):
        url     = 'https://clientbp.ggpolarbear.com/GetLoginData'
        headers = {
            'Expect': '100-continue',
            'Authorization': f'Bearer {JwT_ToKen}',
            'X-Unity-Version': '2022.3.47f1',
            'X-GA': 'v1 1',
            'ReleaseVersion': 'OB53',
            'Content-Type': 'application/x-www-form-urlencoded',
            'User-Agent': 'UnityPlayer/2022.3.47f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)',
            'Host': 'clientbp.ggpolarbear.com',
            'Connection': 'close',
            'Accept-Encoding': 'gzip',
        }
        try:
            res = requests.post(url, headers=headers, data=PayLoad, verify=False)
            data    = json.loads(DeCode_PackEt(res.content.hex()))
            address  = data['32']['data']
            address2 = data['14']['data']
            ip   = address[:len(address) - 6]
            ip2  = address2[:len(address2) - 6]
            port  = address[len(address) - 5:]
            port2 = address2[len(address2) - 5:]
            return ip, port, ip2, port2
        except requests.RequestException as e:
            print(f" - Bad Requests!")
        print(" - Failed To GeT PorTs!")
        return None, None

    def ToKen_GeneRaTe(self, Access_ToKen, Access_Uid):
        url     = "https://loginbp.ggpolarbear.com/MajorLogin"
        headers = {
            'X-Unity-Version': '2022.3.47f1',
            'ReleaseVersion': 'OB53',
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-GA': 'v1 1',
            'Content-Length': '928',
            'User-Agent': 'UnityPlayer/2022.3.47f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)',
            'Host': 'loginbp.ggpolarbear.com',
            'Connection': 'Keep-Alive',
            'Accept-Encoding': 'gzip',
        }
        dT = bytes.fromhex("1a13323032352d30372d33302031313a30323a3531220966726565206669726528043a07312e3132332e31422c416e64726f6964204f5320372e312e32202f204150492d323320284e32473438482f373030323530323234294a0848616e6468656c645207416e64726f69645a045749464960c00c68840772033332307a1f41524d7637205646507633204e454f4e20564d48207c2032343635207c203480019a1b8a010f416472656e6f2028544d292036343092010d4f70656e474c20455320332e319a012b476f6f676c657c31663361643662372d636562342d343934622d383730622d623164616364373230393131a2010c3139372e312e31322e313335aa0102656eb201203939366136323964626364623339363462653662363937386635643831346462ba010134c2010848616e6468656c64ca011073616d73756e6720534d2d473935354eea014066663930633037656239383135616633306134336234613966363031393531366530653463373033623434303932353136643064656661346365663531663261f00101ca0207416e64726f6964d2020457494649ca03203734323862323533646566633136343031386336303461316562626665626466e003daa907e803899b07f003bf0ff803ae088004999b078804daa9079004999b079804daa907c80403d204262f646174612f6170702f636f6d2e6474732e667265656669726574682d312f6c69622f61726de00401ea044832303837663631633139663537663261663465376665666630623234643964397c2f646174612f6170702f636f6d2e6474732e667265656669726574682d312f626173652e61706bf00403f804018a050233329a050a32303139313138363933a80503b205094f70656e474c455332b805ff7fc00504e005dac901ea0507616e64726f6964f2055c4b71734854394748625876574c6668437950416c52526873626d43676542557562555551317375746d525536634e30524f3751453141486e496474385963784d614c575437636d4851322b7374745279377830663935542b6456593d8806019006019a060134a2060134b2060612004a001a00")
        dT = dT.replace(b'2026-01-14 14:1:1:20', str(datetime.now())[:-7].encode())
        dT = dT.replace(
            b'ff90c07eb9815af30a43b4a9f6019516e0e4c703b44092516d0defa4cef51f2a',
            Access_ToKen.encode(),
        )
        dT = dT.replace(b'996a629dbcdb3964be6b6978f5d814db', Access_Uid.encode())

        payload  = bytes.fromhex(EnC_AEs(dT.hex()))
        response = requests.post(url, headers=headers, data=payload, verify=False)

        if response.status_code == 200 and len(response.text) > 10:
            data            = json.loads(DeCode_PackEt(response.content.hex()))
            jwt_token       = data['8']['data']
            combined, key, iv = self.GeT_Key_Iv(response.content)
            ip, port, ip2, port2 = self.GeT_LoGin_PorTs(jwt_token, payload)
            return jwt_token, key, iv, combined, ip, port, ip2, port2
        else:
            print(f"Status: {response.status_code}, Response: {response.content[:200]}")
            print("Lỗi lấy token!")
            sys.exit()

    def Get_FiNal_ToKen_0115(self):
        token, key, iv, Timestamp, ip, port, ip2, port2 = self.Guest_GeneRaTe(self.id, self.password)
        self.JwT_ToKen = token
        try:
            decoded         = jwt.decode(token, options={"verify_signature": False})
            account_uid     = decoded.get('account_id')
            encoded_account = hex(account_uid)[2:]
            hex_value       = DecodE_HeX(Timestamp)
            jwt_hex         = token.encode().hex()
        except Exception as e:
            print(f" - Error In ToKen : {e}")
            return
        try:
            header_len = hex(len(EnC_PacKeT(jwt_hex, key, iv)) // 2)[2:]
            length     = len(encoded_account)
            pad        = '00000000'
            if   length == 9:  pad = '0000000'
            elif length == 8:  pad = '00000000  '
            elif length == 10: pad = '000000'
            elif length == 7:  pad = '000000000'
            else:
                print('Unexpected length encountered')

            header    = f'0115{pad}{encoded_account}{hex_value}00000{header_len}'
            final_tok = header + EnC_PacKeT(jwt_hex, key, iv)
        except Exception as e:
            print(f" - Error In Final Token : {e}")
            return

        self.AutH_ToKen = final_tok
        self.Connect_SerVer(token, final_tok, ip, port, key, iv, ip2, port2)
        return final_tok, key, iv


# ─────────────────────────────────────────────
#  STARTUP
# ─────────────────────────────────────────────

def start_account(account):
    try:
        print(f"[Main] Khởi động account: {account['id']}")
        FF_CLient(account['id'], account['password'])
    except Exception as e:
        print(f"[Main] Lỗi account {account['id']}: {e}")
        time.sleep(5)
        start_account(account)


def StarT_SerVer():
    # 1) Flask API (thread daemon)
    api_thread = threading.Thread(target=run_flask_api, daemon=True)
    api_thread.start()

    # 2) Task Worker (thread daemon — tách hoàn toàn khỏi Flask)
    TaskWorker().start()

    # 3) Các bot FF_CLient
    threads = []
    for account in ACCOUNTS:
        t = threading.Thread(target=start_account, args=(account,))
        t.daemon = True
        threads.append(t)
        t.start()
        time.sleep(3)

    for t in threads:
        t.join()


if __name__ == '__main__':
    StarT_SerVer()
