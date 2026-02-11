import sys
import time
import struct
import threading
import serial
import serial.tools.list_ports
import customtkinter as ctk
from tkinter import filedialog, messagebox

# ================= Настройки интерфейса =================
THEME_SETTINGS = {
    "window_size": "900x600",
    "bg_color": "#242424",          # Основной фон
    "sidebar_color": "#2a2a2a",     # Фон боковой панели
    "button_red": "#CC0000",        # Цвет кнопок
    "button_red_hover": "#ba0000",  # Цвет кнопок при наведении
    "text_color_main": "#FFFFFF",   # Основной текст
    "text_color_console": "#F0F0F0",# Цвет текста в консоли (белый)
    "console_bg": "#000000",        # Фон консоли
    "button_corner_radius": 0,      # 0 делает кнопки плоскими (квадратными)
    "font_family": "Segoe UI"
}
# =========================================================

BAUDRATE = 38400
MAX_FW_SIZE = 120 * 1024
OBFUS = bytes([0x16, 0x6c, 0x14, 0xe6, 0x2e, 0x91, 0x0d, 0x40,
               0x21, 0x35, 0xd5, 0x40, 0x13, 0x03, 0xe9, 0x80])

class FlasherLogic:
    def __init__(self, port, log_callback):
        self.ser = serial.Serial(port, BAUDRATE, timeout=5, write_timeout=5)
        self.buf = bytearray()
        self.log = log_callback

    def xor(self, data, off, sz):
        for i in range(sz):
            data[off + i] ^= OBFUS[i % 16]

    def crc(self, data, off, sz):
        c = 0
        for i in range(sz):
            c ^= data[off + i] << 8
            for _ in range(8):
                c = (c << 1 ^ 0x1021 if c & 0x8000 else c << 1) & 0xFFFF
        return c

    def send(self, msg):
        ln = len(msg) + (len(msg) % 2)
        pkt = bytearray(8 + ln)
        struct.pack_into('<HH', pkt, 0, 0xCDAB, ln)
        pkt[4:4+len(msg)] = msg
        struct.pack_into('<HH', pkt, 4+ln, self.crc(pkt, 4, ln), 0xBADC)
        self.xor(pkt, 4, 2 + ln)
        self.ser.write(pkt)

    def recv(self):
        if len(self.buf) < 8:
            return None
        
        idx = next((i for i in range(len(self.buf)-1) 
                   if self.buf[i:i+2] == b'\xAB\xCD'), -1)
        if idx == -1:
            self.buf = self.buf[-1:] if self.buf and self.buf[-1] == 0xAB else bytearray()
            return None

        if len(self.buf) - idx < 8:
            return None

        ln = struct.unpack_from('<H', self.buf, idx+2)[0]
        end = idx + 6 + ln

        if len(self.buf) < end + 2 or self.buf[end:end+2] != b'\xDC\xBA':
            del self.buf[:idx+2]
            return None

        msg = bytearray(self.buf[idx+4:idx+4+ln+2])
        self.xor(msg, 0, ln + 2)
        del self.buf[:end+2]
        
        return struct.unpack_from('<H', msg, 0)[0], bytes(msg[4:])

    def wait_dev(self):
        acc, last = 0, 0
        for _ in range(500):
            time.sleep(0.01)
            if self.ser.in_waiting:
                self.buf.extend(self.ser.read(self.ser.in_waiting))
            
            m = self.recv()
            if not m or m[0] != 0x0518:
                continue

            now = time.time()
            if 0.005 <= now - last <= 1 and last:
                acc += 1
                if acc >= 5:
                    return m[1][16:32].split(b'\x00')[0].decode('ascii', errors='ignore')
            else:
                acc = 0
            last = now

        raise TimeoutError("Radio not found. Please put it into firmware mode..")

    def handshake(self, ver):
        for _ in range(3):
            time.sleep(0.05)
            if self.ser.in_waiting:
                self.buf.extend(self.ser.read(self.ser.in_waiting))
            if (m := self.recv()) and m[0] == 0x0518:
                msg = bytearray(8)
                struct.pack_into('<HH', msg, 0, 0x0530, 4)
                msg[4:8] = ver[:4].encode('ascii')
                self.send(msg)
        
        time.sleep(0.2)
        while self.recv():
            pass

    def flash(self, data, progress_cb):
        pages = (len(data) + 255) // 256
        ts = int(time.time() * 1000) & 0xFFFFFFFF
        idx = retry = 0

        while idx < pages:
            progress_cb(idx, pages)

            msg = bytearray(272)
            struct.pack_into('<HHIHH', msg, 0, 0x0519, 268, ts, idx, pages)
            off = idx * 256
            msg[16:16+min(256, len(data)-off)] = data[off:off+256]
            self.send(msg)

            ok = False
            for _ in range(300):
                time.sleep(0.01)
                if self.ser.in_waiting:
                    self.buf.extend(self.ser.read(self.ser.in_waiting))
                
                if not (r := self.recv()):
                    continue
                if r[0] == 0x051A:
                    pg, err = struct.unpack_from('<HH', r[1], 4)
                    if pg != idx:
                        continue
                    if err:
                        self.log(f"Error on the block {idx}: {err}")
                        retry += 1
                        if retry > 3:
                            raise RuntimeError(f"Block failure {idx}")
                        break
                    ok, retry = True, 0
                    break

            if ok:
                idx += 1
            else:
                self.log(f"Timeout on block {idx}")
                retry += 1
                if retry > 3:
                    raise RuntimeError(f"Block failure {idx}")

        progress_cb(pages, pages)

    def run_flash(self, data, progress_cb):
        self.log("Waiting for device...")
        ver = self.wait_dev()
        self.log(f"Connected: {ver}")
        
        self.log("Connections in progress...")
        self.handshake(ver)
        
        self.log("Start of firmware...")
        self.flash(data, progress_cb)
        
        self.log("Success The radio station is flashed!")

class FlasherApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("UV-K1 Flasher by OUROBOROS")
        self.geometry(THEME_SETTINGS["window_size"])
        ctk.set_appearance_mode("dark")
        self.configure(fg_color=THEME_SETTINGS["bg_color"])

        # Сетка
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Боковая панель
        self.sidebar = ctk.CTkFrame(self, width=260, corner_radius=0, fg_color=THEME_SETTINGS["sidebar_color"])
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        
        self.label_logo = ctk.CTkLabel(self.sidebar, text="FOR K1/К5V3", 
                                      font=ctk.CTkFont(family=THEME_SETTINGS["font_family"], size=20, weight="bold"))
        self.label_logo.pack(pady=30)

        # Настройки COM-порта
        ctk.CTkLabel(self.sidebar, text="PORT:").pack(pady=(10,0))
        self.port_combo = ctk.CTkComboBox(self.sidebar, values=self.get_ports(), 
                                          corner_radius=THEME_SETTINGS["button_corner_radius"])
        self.port_combo.pack(pady=10, padx=20)

        self.btn_refresh = ctk.CTkButton(self.sidebar, text="UPDATE PORTS", 
                                         fg_color="#333333",
                                         hover_color="#444444",
                                         corner_radius=THEME_SETTINGS["button_corner_radius"],
                                         command=self.refresh_ports)
        self.btn_refresh.pack(pady=5, padx=20)

        # Выбор прошивки
        self.btn_file = ctk.CTkButton(self.sidebar, text="SELECT *.BIN", 
                                      fg_color=THEME_SETTINGS["button_red"],
                                      hover_color=THEME_SETTINGS["button_red_hover"],
                                      corner_radius=THEME_SETTINGS["button_corner_radius"],
                                      command=self.select_file)
        self.btn_file.pack(pady=30, padx=20)
        
        self.lbl_file = ctk.CTkLabel(self.sidebar, text="File: not selected", font=ctk.CTkFont(size=11), wraplength=200)
        self.lbl_file.pack(pady=0)

        # Кнопка СТАРТ
        self.btn_start = ctk.CTkButton(self.sidebar, text="FLASH", 
                                       fg_color=THEME_SETTINGS["button_red"],
                                       hover_color=THEME_SETTINGS["button_red_hover"],
                                       font=ctk.CTkFont(size=18, weight="bold"),
                                       corner_radius=THEME_SETTINGS["button_corner_radius"],
                                       command=self.start_process)
        self.btn_start.pack(side="bottom", pady=40, padx=20, fill="x")

        # Основная область (Консоль)
        self.main_frame = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.main_frame.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)

        self.txt_console = ctk.CTkTextbox(self.main_frame, 
                                          fg_color=THEME_SETTINGS["console_bg"], 
                                          text_color=THEME_SETTINGS["text_color_console"],
                                          font=ctk.CTkFont(family="Consolas", size=13),
                                          corner_radius=0)
        self.txt_console.pack(expand=True, fill="both")

        self.progress = ctk.CTkProgressBar(self.main_frame, 
                                           progress_color=THEME_SETTINGS["button_red"],
                                           fg_color="#222222",
                                           height=15,
                                           corner_radius=0)
        self.progress.set(0)
        self.progress.pack(fill="x", pady=(20, 0))

        self.fw_path = None

    def get_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        return ports if ports else ["NO PORTS"]

    def refresh_ports(self):
        self.port_combo.configure(values=self.get_ports())
        self.add_log("Ports has been updated")

    def select_file(self):
        path = filedialog.askopenfilename(filetypes=[("Binary files", "*.bin")])
        if path:
            self.fw_path = path
            self.lbl_file.configure(text=f"File: {path.split('/')[-1]}")
            self.add_log(f"File uploaded: {path}")

    def add_log(self, text):
        self.txt_console.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n")
        self.txt_console.see("end")

    def update_progress(self, current, total):
        self.progress.set(current / total)
        if current % 10 == 0 or current == total:
            self.add_log(f"Progress: {current}/{total} blocks...")

    def start_process(self):
        if not self.fw_path:
            messagebox.showwarning("Attention", "Select the firmware file!")
            return
        
        port = self.port_combo.get()
        if port == "No ports":
            messagebox.showerror("Error", "COM port not found!")
            return

        self.btn_start.configure(state="disabled")
        self.txt_console.delete("1.0", "end")
        threading.Thread(target=self.flash_worker, args=(port, self.fw_path), daemon=True).start()

    def flash_worker(self, port, path):
        try:
            with open(path, 'rb') as f:
                data = f.read()
            
            if len(data) > MAX_FW_SIZE:
                raise ValueError("The firmware size is too big!")

            flasher = FlasherLogic(port, self.add_log)
            flasher.run_flash(data, self.update_progress)
            messagebox.showinfo("Success", "The radio station is flashed!")
        except Exception as e:
            self.add_log(f"ERROR: {str(e)}")
            messagebox.showerror("Error", f"Crash: {e}")
        finally:
            self.btn_start.configure(state="normal")

if __name__ == "__main__":
    app = FlasherApp()
    app.mainloop()