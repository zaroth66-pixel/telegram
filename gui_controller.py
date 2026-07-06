# gui_controller.py
# Telegram Session Controller — GUI
# Run: python gui_controller.py

import os

import os
import sys
import asyncio
import threading
import json
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

# Try importing tkinter
try:
    import tkinter as tk
    from tkinter import ttk, scrolledtext, filedialog, messagebox
except ImportError:
    print("Tkinter not found. Install: pip install tk")
    sys.exit(1)

# ============ CONFIG ============
API_ID = 22646236 # Replace with your API_ID
API_HASH = '33f8c7697745ab3052a92f513aa857bc'  # Replace with your API_HASH

# ============ GLOBALS ============
client = None
current_session = None
current_dialog = None
listener_running = False
loop = None

# ============ MAIN APP ============
class TelegramGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Telegram Session Controller")
        self.root.geometry("1000x700")
        self.root.configure(bg='#0a1a2b')
        
        # Set icon if available
        try:
            self.root.iconbitmap(default='telegram.ico')
        except:
            pass
        
        # Main container
        self.main_frame = tk.Frame(root, bg='#0a1a2b')
        self.main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Top: Session controls
        self.create_session_frame()
        
        # Middle: Chat list + Message viewer
        self.create_chat_frame()
        
        # Bottom: Message input
        self.create_input_frame()
        
        # Status bar
        self.create_status_bar()
        
        # Bind close event
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        
        # Store loop reference
        global loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Start the async loop in a thread
        self.start_loop_thread()
        
        self.status_var.set("Ready — Load a session to start")
    
    def create_session_frame(self):
        """Top frame: Load session, show account info"""
        frame = tk.Frame(self.main_frame, bg='#0a1a2b')
        frame.pack(fill=tk.X, pady=(0, 10))
        
        # Load button
        self.load_btn = tk.Button(
            frame, text="📁 Load Session", command=self.load_session,
            bg='#4a9eff', fg='#ffffff', font=('Arial', 10, 'bold'),
            padx=15, pady=8, relief=tk.FLAT, cursor='hand2'
        )
        self.load_btn.pack(side=tk.LEFT, padx=(0, 10))
        
        # Account info
        self.account_label = tk.Label(
            frame, text="📱 No session loaded", 
            bg='#0a1a2b', fg='#8ea4b8', font=('Arial', 11)
        )
        self.account_label.pack(side=tk.LEFT, padx=10)
        
        # Refresh button
        self.refresh_btn = tk.Button(
            frame, text="🔄 Refresh", command=self.refresh_chats,
            bg='#1c2c3c', fg='#8ea4b8', font=('Arial', 10),
            padx=10, pady=8, relief=tk.FLAT, cursor='hand2',
            state=tk.DISABLED
        )
        self.refresh_btn.pack(side=tk.RIGHT, padx=5)
        
        # Logout button
        self.logout_btn = tk.Button(
            frame, text="🚪 Logout", command=self.logout,
            bg='#e74c3c', fg='#ffffff', font=('Arial', 10),
            padx=10, pady=8, relief=tk.FLAT, cursor='hand2',
            state=tk.DISABLED
        )
        self.logout_btn.pack(side=tk.RIGHT, padx=5)
    
    def create_chat_frame(self):
        """Middle: Split panes — chats on left, messages on right"""
        paned = tk.PanedWindow(self.main_frame, bg='#0a1a2b', sashwidth=4, sashrelief=tk.FLAT)
        paned.pack(fill=tk.BOTH, expand=True, pady=10)
        
        # Left: Chat list
        left_frame = tk.Frame(paned, bg='#0a1a2b')
        paned.add(left_frame, width=250)
        
        tk.Label(
            left_frame, text="📋 Chats", 
            bg='#0a1a2b', fg='#8ea4b8', font=('Arial', 11, 'bold')
        ).pack(anchor=tk.W, pady=(0, 5))
        
        self.chat_listbox = tk.Listbox(
            left_frame, bg='#17212b', fg='#ffffff', 
            selectbackground='#4a9eff', selectforeground='#ffffff',
            font=('Arial', 10), height=20, relief=tk.FLAT
        )
        self.chat_listbox.pack(fill=tk.BOTH, expand=True)
        self.chat_listbox.bind('<<ListboxSelect>>', self.on_chat_select)
        
        # Right: Messages
        right_frame = tk.Frame(paned, bg='#0a1a2b')
        paned.add(right_frame, width=600)
        
        # Chat title
        self.chat_title_var = tk.StringVar(value="Select a chat")
        tk.Label(
            right_frame, textvariable=self.chat_title_var,
            bg='#0a1a2b', fg='#4a9eff', font=('Arial', 12, 'bold')
        ).pack(anchor=tk.W, pady=(0, 5))
        
        # Message display
        self.msg_display = scrolledtext.ScrolledText(
            right_frame, bg='#17212b', fg='#ffffff',
            font=('Arial', 10), wrap=tk.WORD,
            relief=tk.FLAT, state=tk.DISABLED
        )
        self.msg_display.pack(fill=tk.BOTH, expand=True)
        
        # Configure tags for messages
        self.msg_display.tag_configure('outgoing', foreground='#4a9eff')
        self.msg_display.tag_configure('incoming', foreground='#ffffff')
        self.msg_display.tag_configure('time', foreground='#6f8ba5', font=('Arial', 8))
        self.msg_display.tag_configure('system', foreground='#f39c12', font=('Arial', 9, 'italic'))
    
    def create_input_frame(self):
        """Bottom: Message input and send"""
        frame = tk.Frame(self.main_frame, bg='#0a1a2b')
        frame.pack(fill=tk.X, pady=(10, 0))
        
        # Input field
        self.msg_input = tk.Text(
            frame, bg='#1c2c3c', fg='#ffffff',
            font=('Arial', 10), height=3, relief=tk.FLAT,
            wrap=tk.WORD, insertbackground='#ffffff'
        )
        self.msg_input.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))
        
        # Send button
        self.send_btn = tk.Button(
            frame, text="📤 Send", command=self.send_message,
            bg='#4a9eff', fg='#ffffff', font=('Arial', 11, 'bold'),
            padx=20, pady=10, relief=tk.FLAT, cursor='hand2',
            state=tk.DISABLED
        )
        self.send_btn.pack(side=tk.RIGHT)
        
        # Bind Enter to send
        self.msg_input.bind('<Control-Return>', lambda e: self.send_message())
        self.msg_input.bind('<Return>', lambda e: 'break')
    
    def create_status_bar(self):
        """Status bar at bottom"""
        self.status_var = tk.StringVar(value="Ready")
        status_bar = tk.Label(
            self.root, textvariable=self.status_var,
            bg='#0a1a2b', fg='#6f8ba5', font=('Arial', 9),
            anchor=tk.W, padx=10
        )
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
    
    # ============ ASYNC LOOP THREAD ============
    def start_loop_thread(self):
        """Start the asyncio event loop in a background thread"""
        def run_loop():
            global loop
            asyncio.set_event_loop(loop)
            loop.run_forever()
        
        thread = threading.Thread(target=run_loop, daemon=True)
        thread.start()
    
    def run_async(self, coro):
        """Run a coroutine in the event loop"""
        global loop
        if loop is None:
            return None
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=30)
        except Exception as e:
            raise e
    
    # ============ SESSION HANDLING ============
    def load_session(self):
        """Load a .session file"""
        file_path = filedialog.askopenfilename(
            title="Select Telegram session file",
            filetypes=[("Session files", "*.session"), ("All files", "*.*")]
        )
        if not file_path:
            return
        
        global client, current_session
        
        try:
            self.status_var.set("Loading session...")
            self.root.update()
            
            # Close existing client
            if client:
                self.run_async(client.disconnect())
                client = None
            
            # Create new client
            current_session = file_path
            client = TelegramClient(file_path, API_ID, API_HASH)
            
            # Connect
            self.run_async(client.connect())
            
            # Check if authorized
            is_auth = self.run_async(client.is_user_authorized())
            if not is_auth:
                messagebox.showerror("Error", "Session is not authorized. Please use a valid session file.")
                client = None
                return
            
            # Get account info
            me = self.run_async(client.get_me())
            
            # Update UI
            self.account_label.config(
                text=f"📱 {me.first_name} @{me.username} | {me.phone}"
            )
            self.refresh_btn.config(state=tk.NORMAL)
            self.logout_btn.config(state=tk.NORMAL)
            self.send_btn.config(state=tk.NORMAL)
            
            # Start message listener
            self.start_message_listener()
            
            # Load chats
            self.refresh_chats()
            
            self.status_var.set(f"Connected as {me.first_name}")
            self.log_message(f"✅ Logged in as {me.first_name} @{me.username}", 'system')
            
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load session: {e}")
            self.status_var.set("Error loading session")
            import traceback
            traceback.print_exc()
    
    def start_message_listener(self):
        """Start the message listener for the current client"""
        global client
        
        if not client:
            return
        
        @client.on(events.NewMessage)
        async def handler(event):
            try:
                # Check if this is the currently selected chat
                if current_dialog and event.chat_id == current_dialog.id:
                    self.root.after(0, lambda: self.load_messages(current_dialog))
                
                # Update status
                chat_name = event.chat.title if event.chat else event.sender_id
                self.root.after(0, lambda: self.status_var.set(f"📩 New message from {chat_name}"))
            except Exception as e:
                print(f"Handler error: {e}")
        
        # Keep a reference to prevent garbage collection
        self._listener = handler
        self.status_var.set("Message listener active")
    
    def refresh_chats(self):
        """Refresh the chat list"""
        if not client:
            return
        
        self.status_var.set("Loading chats...")
        self.root.update()
        
        try:
            # Get dialogs
            dialogs = self.run_async(client.get_dialogs())
            
            # Clear listbox
            self.chat_listbox.delete(0, tk.END)
            
            # Store dialogs for later use
            self.dialogs = {}
            for d in dialogs[:50]:
                name = d.name or str(d.id)
                unread = f" ({d.unread_count})" if d.unread_count else ""
                display = f"{name}{unread}"
                self.chat_listbox.insert(tk.END, display)
                self.dialogs[display] = d
            
            self.status_var.set(f"Loaded {len(self.dialogs)} chats")
            
        except Exception as e:
            self.status_var.set(f"Error loading chats: {e}")
            import traceback
            traceback.print_exc()
    
    def on_chat_select(self, event):
        """Handle chat selection"""
        selection = self.chat_listbox.curselection()
        if not selection:
            return
        
        index = selection[0]
        display = self.chat_listbox.get(index)
        dialog = self.dialogs.get(display)
        
        if not dialog:
            return
        
        global current_dialog
        current_dialog = dialog
        
        # Update title
        self.chat_title_var.set(f"💬 {dialog.name}")
        
        # Load messages
        self.load_messages(dialog)
    
    def load_messages(self, dialog):
        """Load messages from selected chat"""
        if not dialog:
            return
        
        self.status_var.set(f"Loading messages from {dialog.name}...")
        self.root.update()
        
        try:
            # Get messages
            messages = self.run_async(client.get_messages(dialog.entity, limit=50))
            
            # Clear display
            self.msg_display.config(state=tk.NORMAL)
            self.msg_display.delete(1.0, tk.END)
            
            # Display messages (oldest first)
            for msg in reversed(messages):
                if not msg:
                    continue
                
                sender = "Me" if msg.out else (msg.sender_id or "Unknown")
                text = msg.text or "[Media]"
                time_str = msg.date.strftime('%H:%M') if msg.date else ''
                
                # Insert message
                tag = 'outgoing' if msg.out else 'incoming'
                self.msg_display.insert(tk.END, f"[{time_str}] {sender}: ", 'time')
                self.msg_display.insert(tk.END, f"{text}\n", tag)
            
            self.msg_display.config(state=tk.DISABLED)
            self.msg_display.see(tk.END)
            
            self.status_var.set(f"Loaded {len(messages)} messages")
            
        except Exception as e:
            self.status_var.set(f"Error loading messages: {e}")
            import traceback
            traceback.print_exc()
    
    def send_message(self):
        """Send a message"""
        if not client or not current_dialog:
            messagebox.showinfo("Info", "Select a chat first")
            return
        
        text = self.msg_input.get(1.0, tk.END).strip()
        if not text:
            return
        
        self.status_var.set("Sending...")
        self.send_btn.config(state=tk.DISABLED)
        self.root.update()
        
        try:
            # Send message
            result = self.run_async(client.send_message(current_dialog.entity, text))
            
            # Clear input
            self.msg_input.delete(1.0, tk.END)
            
            # Refresh messages
            self.load_messages(current_dialog)
            
            self.status_var.set("Message sent")
            
        except Exception as e:
            messagebox.showerror("Error", f"Failed to send: {e}")
            self.status_var.set(f"Error: {e}")
        
        finally:
            self.send_btn.config(state=tk.NORMAL)
    
    def logout(self):
        """Logout and clear session"""
        global client, current_session
        if client:
            try:
                self.run_async(client.disconnect())
            except:
                pass
            client = None
        
        current_session = None
        self.account_label.config(text="📱 No session loaded")
        self.refresh_btn.config(state=tk.DISABLED)
        self.logout_btn.config(state=tk.DISABLED)
        self.send_btn.config(state=tk.DISABLED)
        self.chat_listbox.delete(0, tk.END)
        self.msg_display.config(state=tk.NORMAL)
        self.msg_display.delete(1.0, tk.END)
        self.msg_display.config(state=tk.DISABLED)
        self.chat_title_var.set("Select a chat")
        self.status_var.set("Logged out")
        self.log_message("👋 Logged out", 'system')
    
    def log_message(self, text, tag='system'):
        """Add a log message to the display"""
        self.msg_display.config(state=tk.NORMAL)
        self.msg_display.insert(tk.END, f"[{datetime.now().strftime('%H:%M')}] {text}\n", tag)
        self.msg_display.config(state=tk.DISABLED)
        self.msg_display.see(tk.END)
    
    def on_close(self):
        """Handle window close"""
        global client, loop
        if client:
            try:
                self.run_async(client.disconnect())
            except:
                pass
        if loop:
            loop.stop()
        self.root.destroy()
        sys.exit(0)

# ============ MAIN ============
if __name__ == '__main__':
    root = tk.Tk()
    app = TelegramGUI(root)
    root.mainloop()