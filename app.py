import sys
import socket
import threading
import os
import time
import configparser
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QListWidget, QListWidgetItem, QPushButton, QFileDialog, QLabel, QProgressBar, QHBoxLayout, QInputDialog, QSystemTrayIcon, QMenu
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QEvent
from PyQt6.QtGui import QIcon, QShortcut, QKeySequence, QAction
if sys.platform == "win32":
    try:
        import winsound
    except ImportError:
        winsound = None

# --- Constants ---
# OPTIMISATION: Buffers plus grands pour réduire les appels système. 2MB.
BUFFER_SIZE = 2 * 1024 * 1024
# Plage d'auto‑tuning (512KB ↔ 2MB)
MIN_BUFFER_SIZE = 512 * 1024
MAX_BUFFER_SIZE = 2 * 1024 * 1024
# Fenêtre et seuils d'adaptation
ADAPT_WINDOW_SECONDS = 0.75
ADAPT_IMPROVE_THRESHOLD = 0.10  # +10% -> tenter d'augmenter la taille de chunk
ADAPT_WORSEN_THRESHOLD = 0.10   # -10% -> diminuer la taille de chunk
# OPTIMISATION: Seuil de mise à jour de la barre de progression (tous les 512KB)
UPDATE_BYTES_THRESHOLD = 512 * 1024
# NOUVEAU: Taille des buffers de socket (envoi/réception) alloués par l'OS. 2MB.
SOCKET_BUFFER_SIZE = 2 * 1024 * 1024
SOCKET_TIMEOUT = 10  # Un peu plus de temps pour les grosses connexions
FILE_SIZE_HEADER_LENGTH = 20
TRANSFER_PORT = 8513
DISCOVERY_PORT = 8512
BROADCAST_INTERVAL = 5
APP_VERSION = "V1.3"
STARTUP_REG_NAME = "ZactShare"

DARK_STYLE = """
QWidget {
    background-color: #111827;
    color: #F0F0F0;
    font-family: Arial;
}
QListWidget {
    background-color: #1e293b;
    border: 1px solid #334155;
    padding: 5px;
}
QPushButton {
    background-color: #334155;
    border: 1px solid #475569;
    padding: 5px 10px;
    border-radius: 3px;
}
QPushButton:hover {
    background-color: #475569;
}
QPushButton:pressed {
    background-color: #1e293b;
}
QLabel {
    border: none;
}
QProgressBar {
    border: 1px solid #334155;
    border-radius: 3px;
    text-align: center;
    background-color: #1e293b;
    max-height: 8px;
}
QProgressBar::chunk {
    background-color: #054A6C;
}
"""

class DiscoveryThread(QThread):
    """Thread to listen for discovery announcements on the local network."""
    device_found = pyqtSignal(str, str)  # ip, hostname

    def __init__(self):
        super().__init__()
        self._sock = None

    def stop(self):
        self.requestInterruption()
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass

    def run(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("", DISCOVERY_PORT))
            # Use a very short timeout so the thread reacts to interruption quickly
            self._sock.settimeout(0.2)
            while not self.isInterruptionRequested():
                try:
                    data, addr = self._sock.recvfrom(1024)
                    # Compat: accepter l'ancien format b"discovery_announce" et le nouveau "discovery_announce:<hostname>"
                    if data == b"discovery_announce":
                        self.device_found.emit(addr[0], "")
                        continue
                    hostname = ""
                    try:
                        text = data.decode("utf-8", errors="ignore")
                    except Exception:
                        text = ""
                    if text.startswith("discovery_announce"):
                        # Formats supportés:
                        # - discovery_announce:<name>
                        # - discovery_announce:<name>|<ip>
                        name = ""
                        ip_from_msg = ""
                        parts = text.split(":", 1)
                        if len(parts) == 2:
                            rest = parts[1]
                            subparts = rest.split("|", 1)
                            if len(subparts) == 2:
                                name = subparts[0].strip()
                                ip_from_msg = subparts[1].strip()
                            else:
                                name = rest.strip()
                        use_ip = ip_from_msg or addr[0]
                        self.device_found.emit(use_ip, name)
                except socket.timeout:
                    continue
                except OSError:
                    # Likely closed during shutdown
                    break
                except Exception as e:
                    print(f"Error in discovery thread: {e}")
                    break
        finally:
            try:
                if self._sock is not None:
                    self._sock.close()
            finally:
                self._sock = None

class FileSenderThread(QThread):
    """Thread to send a file to a remote device."""
    progress = pyqtSignal(float, float, float)
    finished = pyqtSignal()

    def __init__(self, file_path, host):
        super().__init__()
        self.file_path = file_path
        self.host = host
        self._sock = None

    def stop(self):
        self.requestInterruption()
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass

    def run(self):
        try:
            file_size = os.path.getsize(self.file_path)
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                # OPTIMISATION: Réglages du socket pour la performance
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_BUFFER_SIZE)
                self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

                self._sock.settimeout(SOCKET_TIMEOUT)
                self._sock.connect((self.host, TRANSFER_PORT))
                
                filename = os.path.basename(self.file_path)
                self._sock.sendall(f"{file_size:<{FILE_SIZE_HEADER_LENGTH}}".encode('utf-8'))
                self._sock.sendall(filename.encode('utf-8') + b'\n')
                
                sent_bytes = 0
                start_time = time.time()
                last_update_bytes = 0
                # Variables pour l'auto-tuning
                current_chunk_size = BUFFER_SIZE
                last_adapt_time = start_time
                last_adapt_bytes = 0
                
                # Tente d'utiliser sendfile (zéro-copie) si disponible pour de meilleures perfs
                with open(self.file_path, "rb") as f:
                    try:
                        offset = 0
                        while not self.isInterruptionRequested() and offset < file_size:
                            to_send = min(current_chunk_size, file_size - offset)
                            sent = self._sock.sendfile(f, offset, to_send)
                            if sent is None:
                                # Certaines implémentations renvoient None => considérer que tout a été envoyé
                                offset = file_size
                                sent_bytes = file_size
                                break
                            if sent == 0:
                                break
                            offset += sent
                            sent_bytes += sent
                            # Adaptation périodique de la taille de chunk
                            now = time.time()
                            if now - last_adapt_time >= ADAPT_WINDOW_SECONDS:
                                window_bytes = sent_bytes - last_adapt_bytes
                                window_speed = window_bytes / (now - last_adapt_time)
                                # Comparer avec la vitesse moyenne précédente
                                total_elapsed = now - start_time
                                avg_speed = sent_bytes / total_elapsed if total_elapsed > 0 else 0
                                if avg_speed > 0:
                                    delta = (window_speed - avg_speed) / avg_speed
                                    if delta > ADAPT_IMPROVE_THRESHOLD and current_chunk_size < MAX_BUFFER_SIZE:
                                        current_chunk_size = min(current_chunk_size * 2, MAX_BUFFER_SIZE)
                                    elif delta < -ADAPT_WORSEN_THRESHOLD and current_chunk_size > MIN_BUFFER_SIZE:
                                        current_chunk_size = max(current_chunk_size // 2, MIN_BUFFER_SIZE)
                                last_adapt_time = now
                                last_adapt_bytes = sent_bytes
                            if sent_bytes - last_update_bytes > UPDATE_BYTES_THRESHOLD or sent_bytes == file_size:
                                elapsed_time = time.time() - start_time
                                speed = sent_bytes / elapsed_time if elapsed_time > 0 else 0
                                self.progress.emit(float(sent_bytes), float(file_size), speed)
                                last_update_bytes = sent_bytes
                    except Exception:
                        # Fallback à l'envoi classique en cas d'indisponibilité de sendfile
                        f.seek(0)
                        buffer = bytearray(BUFFER_SIZE)
                        view = memoryview(buffer)
                        while not self.isInterruptionRequested():
                            # Lire au plus current_chunk_size
                            if len(buffer) != current_chunk_size:
                                buffer = bytearray(current_chunk_size)
                                view = memoryview(buffer)
                            bytes_read = f.readinto(buffer)
                            if not bytes_read:
                                break
                            self._sock.sendall(view[:bytes_read])
                            sent_bytes += bytes_read
                            # Adaptation périodique de la taille de chunk
                            now = time.time()
                            if now - last_adapt_time >= ADAPT_WINDOW_SECONDS:
                                window_bytes = sent_bytes - last_adapt_bytes
                                window_speed = window_bytes / (now - last_adapt_time)
                                total_elapsed = now - start_time
                                avg_speed = sent_bytes / total_elapsed if total_elapsed > 0 else 0
                                if avg_speed > 0:
                                    delta = (window_speed - avg_speed) / avg_speed
                                    if delta > ADAPT_IMPROVE_THRESHOLD and current_chunk_size < MAX_BUFFER_SIZE:
                                        current_chunk_size = min(current_chunk_size * 2, MAX_BUFFER_SIZE)
                                    elif delta < -ADAPT_WORSEN_THRESHOLD and current_chunk_size > MIN_BUFFER_SIZE:
                                        current_chunk_size = max(current_chunk_size // 2, MIN_BUFFER_SIZE)
                                last_adapt_time = now
                                last_adapt_bytes = sent_bytes
                            if sent_bytes - last_update_bytes > UPDATE_BYTES_THRESHOLD or sent_bytes == file_size:
                                elapsed_time = time.time() - start_time
                                speed = sent_bytes / elapsed_time if elapsed_time > 0 else 0
                                self.progress.emit(float(sent_bytes), float(file_size), speed)
                                last_update_bytes = sent_bytes
                        
                        if sent_bytes - last_update_bytes > UPDATE_BYTES_THRESHOLD or sent_bytes == file_size:
                            elapsed_time = time.time() - start_time
                            speed = sent_bytes / elapsed_time if elapsed_time > 0 else 0
                            self.progress.emit(float(sent_bytes), float(file_size), speed)
                            last_update_bytes = sent_bytes
            finally:
                try:
                    if self._sock is not None:
                        self._sock.close()
                finally:
                    self._sock = None
        except Exception as e:
            print(f"Error sending file: {e}")
        finally:
            self.finished.emit()

class FileReceiverThread(QThread):
    """Thread to listen for incoming connections and receive files."""
    reception_started = pyqtSignal(str)
    file_received = pyqtSignal(str)
    progress = pyqtSignal(float, float, float)

    def __init__(self):
        super().__init__()
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(("", TRANSFER_PORT))
        self.server_socket.listen()

    def stop(self):
        self.requestInterruption()
        try:
            self.server_socket.close()
        except Exception:
            pass

    def run(self):
        downloads_path = os.path.join(os.path.expanduser("~"), "Downloads")
        if not os.path.exists(downloads_path):
            os.makedirs(downloads_path)
            
        while not self.isInterruptionRequested():
            try:
                conn, addr = self.server_socket.accept()
                
                # OPTIMISATION: Réglages du socket de connexion pour la performance
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUFFER_SIZE)
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                
                # Créer un thread dédié pour gérer ce client spécifique
                client_handler = threading.Thread(
                    target=self.handle_client,
                    args=(conn, addr, downloads_path)
                )
                client_handler.daemon = True
                client_handler.start()

            except Exception as e:
                if not self.isInterruptionRequested():
                    print(f"Error in receiver loop: {e}")
                break
        
        self.server_socket.close()

    def handle_client(self, conn, addr, downloads_path):
        try:
            with conn:
                conn.settimeout(SOCKET_TIMEOUT)
                
                file_size_bytes = b''
                while len(file_size_bytes) < FILE_SIZE_HEADER_LENGTH:
                    chunk = conn.recv(FILE_SIZE_HEADER_LENGTH - len(file_size_bytes))
                    if not chunk: raise ConnectionError("Connection closed early.")
                    file_size_bytes += chunk
                file_size = int(file_size_bytes.strip().decode('utf-8'))
                
                filename_bytes = b''
                while (byte := conn.recv(1)) not in [b'\n', b'']:
                    filename_bytes += byte
                filename = os.path.basename(filename_bytes.decode('utf-8'))
                
                self.reception_started.emit(f"{filename} from {addr[0]}")

                save_path = self.get_unique_save_path(downloads_path, filename)

                received_bytes = 0
                start_time = time.time()
                last_update_bytes = 0

                with open(save_path, "wb") as f:
                    current_chunk_size = BUFFER_SIZE
                    buffer = bytearray(current_chunk_size)
                    view = memoryview(buffer)
                    last_adapt_time = time.time()
                    last_adapt_bytes = 0
                    while received_bytes < file_size:
                        to_recv = min(current_chunk_size, file_size - received_bytes)
                        if len(buffer) != current_chunk_size:
                            buffer = bytearray(current_chunk_size)
                            view = memoryview(buffer)
                        n = conn.recv_into(view[:to_recv])
                        if not n:
                            break
                        f.write(view[:n])
                        received_bytes += n
                        # Adaptation périodique
                        now = time.time()
                        if now - last_adapt_time >= ADAPT_WINDOW_SECONDS:
                            window_bytes = received_bytes - last_adapt_bytes
                            window_speed = window_bytes / (now - last_adapt_time)
                            total_elapsed = now - start_time
                            avg_speed = received_bytes / total_elapsed if total_elapsed > 0 else 0
                            if avg_speed > 0:
                                delta = (window_speed - avg_speed) / avg_speed
                                if delta > ADAPT_IMPROVE_THRESHOLD and current_chunk_size < MAX_BUFFER_SIZE:
                                    current_chunk_size = min(current_chunk_size * 2, MAX_BUFFER_SIZE)
                                elif delta < -ADAPT_WORSEN_THRESHOLD and current_chunk_size > MIN_BUFFER_SIZE:
                                    current_chunk_size = max(current_chunk_size // 2, MIN_BUFFER_SIZE)
                            last_adapt_time = now
                            last_adapt_bytes = received_bytes
                        
                        if received_bytes - last_update_bytes > UPDATE_BYTES_THRESHOLD or received_bytes == file_size:
                            elapsed_time = time.time() - start_time
                            speed = received_bytes / elapsed_time if elapsed_time > 0 else 0
                            self.progress.emit(float(received_bytes), float(file_size), speed)
                            last_update_bytes = received_bytes
                
                # Émission finale pour s'assurer que la barre de progression atteint 100%
                elapsed_time = time.time() - start_time
                speed = received_bytes / elapsed_time if elapsed_time > 0 else 0
                self.progress.emit(float(received_bytes), float(file_size), speed)
                
                self.file_received.emit(f"File '{os.path.basename(save_path)}' received in Downloads.")
        except socket.timeout:
            self.file_received.emit(f"Connection from {addr[0]} timed out.")
        except Exception as e:
            self.file_received.emit(f"Error receiving file from {addr[0]}: {e}")

    def get_unique_save_path(self, directory, filename):
        save_path = os.path.join(directory, filename)
        counter = 1
        original_save_path = save_path
        while os.path.exists(save_path):
            name, ext = os.path.splitext(original_save_path)
            save_path = f"{name}_{counter}{ext}"
            counter += 1
        return save_path


class App(QWidget):
    # ... (le reste de la classe App reste identique)
    # ... (copiez le reste de la classe App de votre code précédent ici)
    # ... j'inclus la classe complète ci-dessous pour plus de clarté
    """Main application window."""
    def __init__(self):
        super().__init__()
        self.title = "Local File Sharer"
        self.file_path = None
        self.transfer_state = "idle"  # idle | active | finished
        self.threads = []
        self.initUI()

    def initUI(self):
        self.setWindowTitle(self.title)
        layout = QVBoxLayout()

        self.devices_list = QListWidget()
        
        # Layout for the title and version
        # Layout for the title and version
        title_layout = QHBoxLayout()
        title_layout.addWidget(QLabel("Discovered Devices:"))
        title_layout.addStretch()
        title_layout.addWidget(QLabel(APP_VERSION))
        layout.addLayout(title_layout)

        layout.addWidget(self.devices_list)

        self.select_button = QPushButton("Select File")
        self.select_button.clicked.connect(self.select_file)
        layout.addWidget(self.select_button)

        self.send_button = QPushButton("Send File")
        self.send_button.clicked.connect(self.send_file)
        self.send_button.setEnabled(False)
        layout.addWidget(self.send_button)

        self.status_label = QLabel("Status: Idle")
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.setLayout(layout)

        # Configuration: nom local de l'appareil
        self._init_config()
        # Raccourci pour définir le nom: Ctrl+;
        self.name_shortcut = QShortcut(QKeySequence("Ctrl+;"), self)
        self.name_shortcut.activated.connect(self._prompt_for_device_name)

        # Icône de zone de notification (system tray)
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(self.windowIcon())
        self.tray_icon.setToolTip(f"Local File Sharer ({APP_VERSION})")
        tray_menu = QMenu()
        action_show = QAction("Afficher", self)
        action_quit = QAction("Quitter", self)
        action_name = QAction("Définir le nom local", self)
        action_show.triggered.connect(self._show_main_window)
        action_quit.triggered.connect(QApplication.instance().quit)
        action_name.triggered.connect(self._prompt_for_device_name)
        # Démarrage automatique (Windows)
        self.action_autostart = QAction("Démarrer avec Windows", self)
        self.action_autostart.setCheckable(True)
        self.action_autostart.setChecked(self._is_startup_enabled_windows())
        self.action_autostart.toggled.connect(self._toggle_autostart)
        tray_menu.addAction(action_show)
        tray_menu.addAction(action_name)
        tray_menu.addAction(self.action_autostart)
        tray_menu.addSeparator()
        tray_menu.addAction(action_quit)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

        # La gestion des threads est légèrement modifiée pour un arrêt propre
        self.discovery_thread = DiscoveryThread()
        self.discovery_thread.device_found.connect(self.add_device)
        self.threads.append(self.discovery_thread)
        self.discovery_thread.start()

        self.file_receiver_thread = FileReceiverThread()
        self.file_receiver_thread.reception_started.connect(self.on_reception_started)
        self.file_receiver_thread.file_received.connect(self.on_file_received)
        self.file_receiver_thread.progress.connect(self.update_progress)
        self.threads.append(self.file_receiver_thread)
        self.file_receiver_thread.start()

        self.broadcast_timer = threading.Timer(BROADCAST_INTERVAL, self.broadcast_discovery)
        self.broadcast_timer.daemon = True
        self.broadcast_timer.start()

        self.devices_list.itemSelectionChanged.connect(self.update_send_button_state)
    
    def update_send_button_state(self):
        """Enables/disables the send button based on list selection and file path."""
        is_selected = len(self.devices_list.selectedItems()) > 0 and self.file_path is not None
        self.send_button.setEnabled(is_selected)

    def broadcast_discovery(self):
        """Sends UDP discovery packets."""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                # Utilise le nom local configuré, inclut aussi l'IP locale pour éviter les NAT/bind ambigus
                name = getattr(self, 'device_name', None) or 'PC'
                try:
                    # Détermination IP locale
                    tmp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    tmp.connect(("8.8.8.8", 80))
                    local_ip = tmp.getsockname()[0]
                    tmp.close()
                except Exception:
                    local_ip = ""
                payload_text = f"discovery_announce:{name}|{local_ip}" if local_ip else f"discovery_announce:{name}"
                payload = payload_text.encode("utf-8", errors="ignore")
                # Nouveau format avec nom et IP
                s.sendto(payload, ("<broadcast>", DISCOVERY_PORT))
            except Exception as e:
                print(f"Broadcast error: {e}") # Peut arriver si pas de réseau

        # Si on veut déclencher un nouveau timer, c'est géré plus bas
        
        # Continue le broadcast tant que l'application est en cours d'exécution
            self.broadcast_timer = threading.Timer(BROADCAST_INTERVAL, self.broadcast_discovery)
            self.broadcast_timer.daemon = True
            self.broadcast_timer.start()

    def _show_main_window(self):
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            # Clic simple: toggle
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self._show_main_window()

    def _init_config(self):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.config_path = os.path.join(script_dir, 'config.ini')
        self.config = configparser.ConfigParser()
        self.device_name = 'PC'
        self.start_on_boot = False
        if os.path.exists(self.config_path):
            try:
                self.config.read(self.config_path, encoding='utf-8')
                self.device_name = self.config.get('app', 'device_name', fallback='PC').strip() or 'PC'
                sob = self.config.get('app', 'start_on_boot', fallback='false').strip().lower()
                self.start_on_boot = sob in ('1', 'true', 'yes', 'on')
            except Exception:
                self.device_name = 'PC'
                self.start_on_boot = False
        else:
            self._save_config_device_name(self.device_name)
            self._save_config_start_on_boot(self.start_on_boot)
        # Appliquer l'état de démarrage automatique si Windows
        try:
            self._set_startup_windows(self.start_on_boot)
        except Exception:
            pass

    def _save_config_device_name(self, name: str):
        try:
            if not self.config.has_section('app'):
                self.config.add_section('app')
            self.config.set('app', 'device_name', name)
            with open(self.config_path, 'w', encoding='utf-8') as f:
                self.config.write(f)
        except Exception as e:
            print(f"Failed to save config: {e}")

    def _save_config_start_on_boot(self, enabled: bool):
        try:
            if not self.config.has_section('app'):
                self.config.add_section('app')
            self.config.set('app', 'start_on_boot', 'true' if enabled else 'false')
            with open(self.config_path, 'w', encoding='utf-8') as f:
                self.config.write(f)
        except Exception as e:
            print(f"Failed to save config: {e}")

    def _is_startup_enabled_windows(self) -> bool:
        if sys.platform != 'win32':
            return False
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\\Microsoft\\Windows\\CurrentVersion\\Run", 0, winreg.KEY_READ) as key:
                try:
                    _val, _ = winreg.QueryValueEx(key, STARTUP_REG_NAME)
                    return True
                except FileNotFoundError:
                    return False
        except Exception:
            return False

    def _get_startup_command_string(self) -> str:
        # Prépare la commande pour lancer cette app au login
        if getattr(sys, 'frozen', False):
            # Exe packagé (PyInstaller)
            return f'"{sys.executable}"'
        # Script Python: préférer pythonw.exe si disponible
        python_exe = sys.executable
        if sys.platform == 'win32':
            try:
                base = os.path.dirname(python_exe)
                pythonw = os.path.join(base, 'pythonw.exe')
                if os.path.exists(pythonw):
                    python_exe = pythonw
            except Exception:
                pass
        script = os.path.abspath(__file__)
        return f'"{python_exe}" "{script}"'

    def _set_startup_windows(self, enabled: bool):
        if sys.platform != 'win32':
            return
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\\Microsoft\\Windows\\CurrentVersion\\Run", 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    cmd = self._get_startup_command_string()
                    winreg.SetValueEx(key, STARTUP_REG_NAME, 0, winreg.REG_SZ, cmd)
                else:
                    try:
                        winreg.DeleteValue(key, STARTUP_REG_NAME)
                    except FileNotFoundError:
                        pass
        except Exception as e:
            print(f"Failed to update startup setting: {e}")

    def _toggle_autostart(self, checked: bool):
        self.start_on_boot = bool(checked)
        self._save_config_start_on_boot(self.start_on_boot)
        self._set_startup_windows(self.start_on_boot)

    def _prompt_for_device_name(self):
        text, ok = QInputDialog.getText(self, "Nom de l'appareil", "Entrez le nom local à annoncer:", text=self.device_name)
        if ok:
            new_name = (text or '').strip() or 'PC'
            if new_name != self.device_name:
                self.device_name = new_name
                self._save_config_device_name(self.device_name)
                self.status_label.setText(f"Nom local défini: {self.device_name}")
                # Envoyer une annonce immédiate (nouveau format uniquement)
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                        # Inclure l'IP locale si possible
                        try:
                            tmp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                            tmp.connect(("8.8.8.8", 80))
                            local_ip = tmp.getsockname()[0]
                            tmp.close()
                        except Exception:
                            local_ip = ""
                        payload_text = f"discovery_announce:{self.device_name}|{local_ip}" if local_ip else f"discovery_announce:{self.device_name}"
                        payload = payload_text.encode("utf-8", errors="ignore")
                        s.sendto(payload, ("<broadcast>", DISCOVERY_PORT))
                except Exception:
                    pass

    def add_device(self, ip, hostname):
        """Adds a discovered device (ip, hostname) to the list if it's not the local device."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            if ip != local_ip:
                # Check duplicates by stored IP in item data
                for i in range(self.devices_list.count()):
                    item = self.devices_list.item(i)
                    if item.data(Qt.ItemDataRole.UserRole) == ip:
                        # Ne pas écraser un nom connu par "Unknown"
                        if hostname and hostname.strip():
                            display_name = f"{ip} - {hostname}"
                            if item.text() != display_name:
                                item.setText(display_name)
                        break
                else:
                    display_name = f"{ip} - {hostname.strip() if (hostname and hostname.strip()) else 'Unknown'}"
                    item = QListWidgetItem(display_name)
                    item.setData(Qt.ItemDataRole.UserRole, ip)
                    self.devices_list.addItem(item)
        except Exception:
            # Si pas de connexion internet, on ne peut pas déterminer l'IP locale, on ajoute tout
            for i in range(self.devices_list.count()):
                item = self.devices_list.item(i)
                if item.data(Qt.ItemDataRole.UserRole) == ip:
                    if hostname and hostname.strip():
                        display_name = f"{ip} - {hostname}"
                        if item.text() != display_name:
                            item.setText(display_name)
                    break
            else:
                display_name = f"{ip} - {hostname.strip() if (hostname and hostname.strip()) else 'Unknown'}"
                item = QListWidgetItem(display_name)
                item.setData(Qt.ItemDataRole.UserRole, ip)
                self.devices_list.addItem(item)

    def select_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Select File")
        if file_path:
            self.file_path = file_path
            self.status_label.setText(f"Selected: {os.path.basename(file_path)}")
            self.update_send_button_state()

    def send_file(self):
        selected_items = self.devices_list.selectedItems()
        if not selected_items or not self.file_path:
            self.status_label.setText("Status: Select a file and a device.")
            return

        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self.send_button.setEnabled(False)
        self.select_button.setEnabled(False)
        self.transfer_state = "active"

        item = selected_items[0]
        host = item.data(Qt.ItemDataRole.UserRole) or item.text().split(" ", 1)[0]
        file_sender_thread = FileSenderThread(self.file_path, host)
        file_sender_thread.progress.connect(self.update_progress)
        file_sender_thread.finished.connect(self.on_send_finished)
        self.threads.append(file_sender_thread) # Garder une référence
        file_sender_thread.start()
        self.status_label.setText(f"Sending {os.path.basename(self.file_path)} to {host}...")

    def update_progress(self, current, total, speed):
        """Updates the UI with progress, speed, and ETA."""
        if total > 0:
            scaled_total = int(total // 1024)
            scaled_current = int(current // 1024)

            if scaled_total > 0 and self.progress_bar.maximum() != scaled_total:
                self.progress_bar.setMaximum(scaled_total)

            # Réduire la fréquence de mise à jour pour de meilleures perfs
            self.progress_bar.setValue(scaled_current)

            percent = (current / total) * 100
            speed_mbps = speed / (1024 * 1024)

            if speed > 0 and current < total:
                remaining_bytes = total - current
                remaining_seconds = int(remaining_bytes / speed)
                minutes, seconds = divmod(remaining_seconds, 60)
                eta_str = f"{minutes}m {seconds:02}s"
                self.status_label.setText(f"Transferring... {percent:.1f}% at {speed_mbps:.2f} MB/s (ETA: {eta_str})")
            else:
                self.status_label.setText(f"Transferring... {percent:.1f}% at {speed_mbps:.2f} MB/s")

    def on_reception_started(self, filename):
        # Afficher la fenêtre et la centrer à la réception d'un transfert
        self._show_and_center()
        self.status_label.setText(f"Receiving {filename}...")
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self.transfer_state = "active"

    def on_send_finished(self):
        self.status_label.setText("Status: File sent successfully!")
        self.progress_bar.setVisible(False)
        self.send_button.setEnabled(True)
        self.select_button.setEnabled(True)
        self.update_send_button_state()
        if winsound:
            winsound.MessageBeep(winsound.MB_OK)
        self.transfer_state = "finished"

    def on_file_received(self, status_message):
        self.status_label.setText(f"Status: {status_message}")
        self.progress_bar.setVisible(False)
        if "successfully received" in status_message and winsound:
            winsound.MessageBeep(winsound.MB_OK)
        self.transfer_state = "finished"

    def showEvent(self, event):
        super().showEvent(event)
        if sys.platform == "win32": self._set_dark_title_bar()

    def changeEvent(self, event):
        # Masquer dans la zone de notification lors de la minimisation
        if event.type() == QEvent.Type.WindowStateChange:
            if self.isMinimized():
                self.hide()
                try:
                    if self.tray_icon and self.tray_icon.isVisible():
                        self.tray_icon.showMessage("Local File Sharer", "Application minimisée dans la zone de notification.", QSystemTrayIcon.MessageIcon.Information, 2000)
                except Exception:
                    pass
                # Remettre le statut à Idle uniquement si un transfert est terminé
                if self.transfer_state == "finished":
                    try:
                        self.status_label.setText("Status: Idle")
                        self.transfer_state = "idle"
                    except Exception:
                        pass
        super().changeEvent(event)

    def _set_dark_title_bar(self):
        try:
            import ctypes
            hwnd = self.winId()
            if not hwnd: return
            value = ctypes.c_int(2)
            try:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
            except ctypes.ArgumentError:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(value), ctypes.sizeof(value))
        except Exception as e:
            pass

    def _show_and_center(self):
        try:
            self.showNormal()
            self.activateWindow()
            self.raise_()
            # Centrer au milieu de l'écran disponible
            screen = self.screen() or QApplication.primaryScreen()
            if screen is not None:
                avail = screen.availableGeometry()
                frame = self.frameGeometry()
                frame.moveCenter(avail.center())
                self.move(frame.topLeft())
        except Exception:
            # En cas de souci, au moins rendre visible
            try:
                self.showNormal()
            except Exception:
                pass

    def closeEvent(self, event):
        # OPTIMISATION: Arrêt plus propre des threads
        try:
            self.broadcast_timer.cancel()
        except Exception:
            pass
        
        # Demander aux threads de s'arrêter immédiatement
        try:
            self.discovery_thread.stop()
        except Exception:
            pass

        try:
            self.file_receiver_thread.stop()
        except Exception:
            pass

        for thread in self.threads:
            if isinstance(thread, FileSenderThread):
                try:
                    thread.stop()
                except Exception:
                    pass

        for thread in self.threads:
            if isinstance(thread, QThread):
                thread.requestInterruption()
        
        # Attendre qu'ils se terminent
        for thread in self.threads:
             if isinstance(thread, QThread):
                thread.wait(300) # Attendre brièvement
        
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    logo_path = os.path.join(script_dir, 'logo.png')
    if os.path.exists(logo_path):
        app.setWindowIcon(QIcon(logo_path))

    app.setStyleSheet(DARK_STYLE)
    ex = App()
    ex.show()
    sys.exit(app.exec())