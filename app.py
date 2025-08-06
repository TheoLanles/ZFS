import sys
import socket
import threading
import os
import time
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QListWidget, QPushButton, QFileDialog, QLabel, QProgressBar, QHBoxLayout
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtGui import QIcon
if sys.platform == "win32":
    try:
        import winsound
    except ImportError:
        winsound = None

# --- Constants ---
# OPTIMISATION: Augmentation radicale du buffer pour moins d'appels système. 1MB.
BUFFER_SIZE = 1024 * 1024
# OPTIMISATION: Seuil de mise à jour de la barre de progression (tous les 256KB)
UPDATE_BYTES_THRESHOLD = 64 * 1024
# NOUVEAU: Taille des buffers de socket (envoi/réception) alloués par l'OS. 2MB.
SOCKET_BUFFER_SIZE = 2 * 1024 * 1024
SOCKET_TIMEOUT = 10  # Un peu plus de temps pour les grosses connexions
FILE_SIZE_HEADER_LENGTH = 20
TRANSFER_PORT = 8513
DISCOVERY_PORT = 8512
BROADCAST_INTERVAL = 5
APP_VERSION = "V1.2"

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
    device_found = pyqtSignal(str)

    def run(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", DISCOVERY_PORT))
            s.settimeout(SOCKET_TIMEOUT)
            while not self.isInterruptionRequested():
                try:
                    data, addr = s.recvfrom(1024)
                    if data == b"discovery_announce":
                        self.device_found.emit(addr[0])
                except (OSError, socket.timeout):
                    continue
                except Exception as e:
                    print(f"Error in discovery thread: {e}")
                    break

class FileSenderThread(QThread):
    """Thread to send a file to a remote device."""
    progress = pyqtSignal(float, float, float)
    finished = pyqtSignal()

    def __init__(self, file_path, host):
        super().__init__()
        self.file_path = file_path
        self.host = host

    def run(self):
        try:
            file_size = os.path.getsize(self.file_path)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                # OPTIMISATION: Réglages du socket pour la performance
                s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_BUFFER_SIZE)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

                s.settimeout(SOCKET_TIMEOUT)
                s.connect((self.host, TRANSFER_PORT))
                
                filename = os.path.basename(self.file_path)
                s.sendall(f"{file_size:<{FILE_SIZE_HEADER_LENGTH}}".encode('utf-8'))
                s.sendall(filename.encode('utf-8') + b'\n')
                
                sent_bytes = 0
                start_time = time.time()
                last_update_bytes = 0
                
                with open(self.file_path, "rb") as f:
                    while (chunk := f.read(BUFFER_SIZE)):
                        s.sendall(chunk) # sendall est déjà optimisé
                        sent_bytes += len(chunk)
                        
                        if sent_bytes - last_update_bytes > UPDATE_BYTES_THRESHOLD or sent_bytes == file_size:
                            elapsed_time = time.time() - start_time
                            speed = sent_bytes / elapsed_time if elapsed_time > 0 else 0
                            self.progress.emit(float(sent_bytes), float(file_size), speed)
                            last_update_bytes = sent_bytes
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
                    while received_bytes < file_size:
                        chunk = conn.recv(BUFFER_SIZE)
                        if not chunk: break
                        f.write(chunk)
                        received_bytes += len(chunk)
                        
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
                s.sendto(b"discovery_announce", ("<broadcast>", DISCOVERY_PORT))
            except Exception as e:
                print(f"Broadcast error: {e}") # Peut arriver si pas de réseau
        
        if self.isVisible(): # Continue le broadcast uniquement si la fenêtre est ouverte
            self.broadcast_timer = threading.Timer(BROADCAST_INTERVAL, self.broadcast_discovery)
            self.broadcast_timer.daemon = True
            self.broadcast_timer.start()

    def add_device(self, device):
        """Adds a discovered device to the list if it's not the local device."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            if device != local_ip and not self.devices_list.findItems(device, Qt.MatchFlag.MatchExactly):
                self.devices_list.addItem(device)
        except Exception:
            # Si pas de connexion internet, on ne peut pas déterminer l'IP locale, on ajoute tout
            if not self.devices_list.findItems(device, Qt.MatchFlag.MatchExactly):
                self.devices_list.addItem(device)

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

        host = selected_items[0].text()
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
        self.status_label.setText(f"Receiving {filename}...")
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)

    def on_send_finished(self):
        self.status_label.setText("Status: File sent successfully!")
        self.progress_bar.setVisible(False)
        self.send_button.setEnabled(True)
        self.select_button.setEnabled(True)
        self.update_send_button_state()
        if winsound:
            winsound.MessageBeep(winsound.MB_OK)

    def on_file_received(self, status_message):
        self.status_label.setText(f"Status: {status_message}")
        self.progress_bar.setVisible(False)
        if "successfully received" in status_message and winsound:
            winsound.MessageBeep(winsound.MB_OK)

    def showEvent(self, event):
        super().showEvent(event)
        if sys.platform == "win32": self._set_dark_title_bar()

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
            print(f"Failed to set dark title bar: {e}")

    def closeEvent(self, event):
        # OPTIMISATION: Arrêt plus propre des threads
        self.broadcast_timer.cancel()
        
        # Demander aux threads de s'arrêter
        self.file_receiver_thread.server_socket.close() # Débloque le .accept()
        for thread in self.threads:
            if isinstance(thread, QThread):
                thread.requestInterruption()
        
        # Attendre qu'ils se terminent
        for thread in self.threads:
             if isinstance(thread, QThread):
                thread.wait(2000) # Attendre 2 secondes max
        
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