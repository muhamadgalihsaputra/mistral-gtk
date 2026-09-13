import os
import sys
import json
import re
import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('WebKit', '6.0')
gi.require_version('GLib', '2.0')
gi.require_version('Gdk', '4.0')
gi.require_version('Gio', '2.0')

from gi.repository import Gtk, Adw, WebKit, GLib, Gdk, Gio
from mistral_gtk.tray import StatusNotifierTray

APP_ID = "io.github.mistral_gtk.desktop"
DEFAULT_URL = "https://chat.mistral.ai/work"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.3 Safari/605.1.15"
)

AUTH_DOMAINS = (
    "accounts.google.com",
    "appleid.apple.com",
    "login.microsoftonline.com",
    "login.live.com",
    "github.com",
    "auth0.com",
    "auth.mistral.ai",
    "clerk.com",
    "okta.com",
)


def is_auth_url(url_str):
    if not url_str:
        return False
    try:
        from urllib.parse import urlparse
        p = urlparse(url_str)
        host = p.netloc.lower()
        if any(ad in host for ad in AUTH_DOMAINS):
            return True
        path = p.path.lower()
        if any(kw in path for kw in ("/auth/", "/login", "/signin", "/oauth", "/sso")):
            return True
    except Exception:
        pass
    return False


def is_same_app_domain(url_str):
    if not url_str:
        return False
    try:
        from urllib.parse import urlparse
        host = urlparse(url_str).netloc.lower()
        return any(d in host for d in ("mistral.ai", "console.mistral.ai", "chat.mistral.ai"))
    except Exception:
        return False


STEALTH_SCRIPT = """
try {
    Object.defineProperty(navigator, 'webdriver', {
        get: () => false,
        configurable: true
    });
} catch(e) {}
"""

# Auto-focus the chat input field with continuous polling, DOM observer, and window focus hooks.
FOCUS_SCRIPT = """
(function() {
    function focusInput() {
        const selectors = [
            'textarea[tabindex="0"]',
            'textarea',
            '[contenteditable="true"]',
            'input[type="text"]'
        ];
        for (const s of selectors) {
            const el = document.querySelector(s);
            if (el && el.offsetParent !== null && !el.disabled) {
                el.focus();
                return true;
            }
        }
        return false;
    }

    let count = 0;
    const interval = setInterval(() => {
        count++;
        if (focusInput() || count > 30) {
            clearInterval(interval);
        }
    }, 200);

    window.addEventListener('focus', () => {
        focusInput();
    });

    if (window.MutationObserver) {
        let obsCount = 0;
        const observer = new MutationObserver(() => {
            obsCount++;
            if (focusInput() || obsCount > 25) {
                observer.disconnect();
            }
        });
        observer.observe(document.documentElement, { childList: true, subtree: true });
        setTimeout(() => observer.disconnect(), 10000);
    }
})();
"""

# Track LLM streaming/generation state and notify host when complete
GENERATION_SCRIPT = """
(function() {
    let wasGenerating = false;

    function checkGenerating() {
        const stopBtn = document.querySelector(
            'button[aria-label*="Stop"], ' +
            'button[aria-label*="Arrêter"], ' +
            'button[aria-label*="Berhenti"], ' +
            'button[data-testid*="stop"]'
        );
        const isGenerating = !!stopBtn;

        if (wasGenerating && !isGenerating) {
            if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.generation_done) {
                window.webkit.messageHandlers.generation_done.postMessage("done");
            }
        }
        wasGenerating = isGenerating;
    }

    setInterval(checkGenerating, 400);
})();
"""

DATA_DIR = os.path.expanduser("~/.local/share/mistral-gtk")
CACHE_DIR = os.path.expanduser("~/.cache/mistral-gtk")
CONFIG_DIR = os.path.expanduser("~/.config/mistral-gtk")
CONFIG_FILE = os.path.join(CONFIG_DIR, "window_state.json")
DOWNLOAD_DIR = os.path.expanduser("~/Downloads")


def load_window_state():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"width": 1080, "height": 800, "is_maximized": False}


def save_window_state(width, height, is_maximized):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"width": width, "height": height, "is_maximized": is_maximized}, f)
    except Exception:
        pass


class MistralWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Mistral")
        self.app = app

        # Window sizing & state
        state = load_window_state()
        self.set_default_size(state.get("width", 1080), state.get("height", 800))
        if state.get("is_maximized", False):
            self.maximize()

        # Tray notification dedup state
        self._last_notified_title = None
        self.connect("notify::visible", self.on_visibility_changed)

        # Intercept window close -> hide to system tray
        self.connect("close-request", self.on_close_request)

        # Persistent Network Session
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(CACHE_DIR, exist_ok=True)
        self.session = WebKit.NetworkSession.new(
            data_directory=DATA_DIR,
            cache_directory=CACHE_DIR
        )

        # Disable ITP and ensure cross-origin cookies
        self.session.set_itp_enabled(False)
        cookie_manager = self.session.get_cookie_manager()
        cookie_manager.set_accept_policy(WebKit.CookieAcceptPolicy.ALWAYS)
        cookie_file = os.path.join(DATA_DIR, "cookies.sqlite")
        cookie_manager.set_persistent_storage(
            cookie_file, WebKit.CookiePersistentStorage.SQLITE
        )

        # WebKit Settings
        self.settings = WebKit.Settings()
        self.settings.set_user_agent(USER_AGENT)
        self.settings.set_enable_developer_extras(True)
        self.settings.set_enable_webrtc(True)
        self.settings.set_enable_media_stream(True)
        self.settings.set_javascript_can_access_clipboard(True)
        self.settings.set_javascript_can_open_windows_automatically(True)

        # User Content Manager for Anti-Bot / Stealth script injection
        self.user_content_manager = WebKit.UserContentManager()
        stealth_user_script = WebKit.UserScript(
            source=STEALTH_SCRIPT,
            injected_frames=WebKit.UserContentInjectedFrames.ALL_FRAMES,
            injection_time=WebKit.UserScriptInjectionTime.START
        )
        self.user_content_manager.add_script(stealth_user_script)

        # Auto-focus chat input on every page load
        focus_user_script = WebKit.UserScript(
            source=FOCUS_SCRIPT,
            injected_frames=WebKit.UserContentInjectedFrames.ALL_FRAMES,
            injection_time=WebKit.UserScriptInjectionTime.END
        )
        self.user_content_manager.add_script(focus_user_script)

        # Monitor LLM generation completion
        generation_user_script = WebKit.UserScript(
            source=GENERATION_SCRIPT,
            injected_frames=WebKit.UserContentInjectedFrames.ALL_FRAMES,
            injection_time=WebKit.UserScriptInjectionTime.END
        )
        self.user_content_manager.add_script(generation_user_script)
        self.user_content_manager.register_script_message_handler("generation_done")
        self.user_content_manager.connect(
            "script-message-received::generation_done",
            self.on_generation_done
        )

        # Ensure keyboard focus is on web_view when window becomes active
        self.connect("notify::is-active", self.on_window_active_changed)

        # Main Layout Box
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(self.main_box)

        # HeaderBar (Inherits GNOME theme & left button-layout)
        self.header_bar = Adw.HeaderBar()
        self.header_bar.add_css_class("flat")
        self.main_box.append(self.header_bar)

        # Window Title Widget
        self.title_widget = Adw.WindowTitle(title="Mistral", subtitle="chat.mistral.ai")
        self.header_bar.set_title_widget(self.title_widget)

        # Navigation Controls (Left side of header, right after traffic lights)
        self.nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.header_bar.pack_start(self.nav_box)

        self.btn_back = Gtk.Button(icon_name="go-previous-symbolic")
        self.btn_back.set_tooltip_text("Kembali (Alt+Left)")
        self.btn_back.connect("clicked", self.on_back_clicked)
        self.btn_back.set_sensitive(False)
        self.nav_box.append(self.btn_back)

        self.btn_forward = Gtk.Button(icon_name="go-next-symbolic")
        self.btn_forward.set_tooltip_text("Maju (Alt+Right)")
        self.btn_forward.connect("clicked", self.on_forward_clicked)
        self.btn_forward.set_sensitive(False)
        self.nav_box.append(self.btn_forward)

        self.btn_reload = Gtk.Button(icon_name="view-refresh-symbolic")
        self.btn_reload.set_tooltip_text("Muat Ulang (Ctrl+R)")
        self.btn_reload.connect("clicked", self.on_reload_clicked)
        self.nav_box.append(self.btn_reload)

        self.btn_home = Gtk.Button(icon_name="go-home-symbolic")
        self.btn_home.set_tooltip_text("Beranda Mistral")
        self.btn_home.connect("clicked", lambda _: self.web_view.load_uri(DEFAULT_URL))
        self.nav_box.append(self.btn_home)

        # Right side actions (Menu button)
        self.menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic")
        self.menu_btn.set_tooltip_text("Menu Aplikasi")
        self.header_bar.pack_end(self.menu_btn)
        self.setup_menu()

        # Progress bar at the top of content
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.add_css_class("osd")
        self.progress_bar.set_visible(False)
        self.main_box.append(self.progress_bar)

        # Primary WebView
        self.web_view = WebKit.WebView(
            network_session=self.session,
            user_content_manager=self.user_content_manager
        )
        self.web_view.set_settings(self.settings)
        self.web_view.set_vexpand(True)
        self.web_view.set_hexpand(True)
        self.main_box.append(self.web_view)

        # Connect WebView Signals
        self.web_view.connect("notify::estimated-load-progress", self.on_progress_changed)
        self.web_view.connect("notify::title", self.on_title_changed)
        self.web_view.connect("notify::uri", self.on_uri_changed)
        self.web_view.connect("load-changed", self.on_load_changed)
        self.web_view.connect("create", self.on_create_popup)
        self.web_view.connect("permission-request", self.on_permission_request)
        self.web_view.connect("show-notification", self.on_show_notification)
        self.web_view.connect("decide-policy", self.on_decide_policy)
        self.session.connect("download-started", self.on_download_started)

        # Key controller for shortcuts
        self.setup_shortcuts()

        # Defer initial load so window displays instantly without blocking
        GLib.idle_add(lambda: self.web_view.load_uri(DEFAULT_URL))

    def setup_menu(self):
        menu = Gio.Menu()

        # View Section
        view_section = Gio.Menu()
        view_section.append("Perbesar (Ctrl++)", "app.zoom_in")
        view_section.append("Perkecil (Ctrl+-)", "app.zoom_out")
        view_section.append("Reset Zoom (Ctrl+0)", "app.zoom_reset")
        menu.append_section(None, view_section)

        # Tools Section
        tools_section = Gio.Menu()
        tools_section.append("Salin URL Halaman", "app.copy_url")
        tools_section.append("Buka Web Inspector (F12)", "app.inspect")
        tools_section.append("Hapus Cache", "app.clear_cache")
        menu.append_section(None, tools_section)

        # App Section
        app_section = Gio.Menu()
        app_section.append("Tentang Mistral GTK", "app.about")
        app_section.append("Sembunyikan ke Tray", "app.hide_to_tray")
        app_section.append("Keluar (Ctrl+Q)", "app.quit")
        menu.append_section(None, app_section)

        self.menu_btn.set_menu_model(menu)

    def setup_shortcuts(self):
        controller = Gtk.EventControllerKey()
        controller.connect("key-pressed", self.on_key_pressed)
        self.add_controller(controller)

    def on_key_pressed(self, controller, keyval, keycode, state):
        ctrl = (state & Gdk.ModifierType.CONTROL_MASK) != 0
        alt = (state & Gdk.ModifierType.ALT_MASK) != 0

        if ctrl:
            if keyval in (Gdk.KEY_r, Gdk.KEY_R):
                self.web_view.reload()
                return True
            elif keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
                self.zoom_in()
                return True
            elif keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
                self.zoom_out()
                return True
            elif keyval in (Gdk.KEY_0, Gdk.KEY_KP_0):
                self.zoom_reset()
                return True
            elif keyval in (Gdk.KEY_q, Gdk.KEY_Q):
                self.app.quit()
                return True
            elif keyval in (Gdk.KEY_w, Gdk.KEY_W):
                self.set_visible(False)
                return True
        elif alt:
            if keyval == Gdk.KEY_Left:
                if self.web_view.can_go_back():
                    self.web_view.go_back()
                return True
            elif keyval == Gdk.KEY_Right:
                if self.web_view.can_go_forward():
                    self.web_view.go_forward()
                return True
        elif keyval == Gdk.KEY_F11:
            if self.is_fullscreen():
                self.unfullscreen()
            else:
                self.fullscreen()
            return True
        elif keyval == Gdk.KEY_F12:
            inspector = self.web_view.get_inspector()
            if inspector.is_attached():
                inspector.close()
            else:
                inspector.show()
            return True

        return False

    def zoom_in(self):
        level = self.web_view.get_zoom_level()
        self.web_view.set_zoom_level(min(level + 0.1, 3.0))

    def zoom_out(self):
        level = self.web_view.get_zoom_level()
        self.web_view.set_zoom_level(max(level - 0.1, 0.5))

    def zoom_reset(self):
        self.web_view.set_zoom_level(1.0)

    def on_back_clicked(self, _):
        if self.web_view.can_go_back():
            self.web_view.go_back()

    def on_forward_clicked(self, _):
        if self.web_view.can_go_forward():
            self.web_view.go_forward()

    def on_reload_clicked(self, _):
        self.web_view.reload()

    def on_progress_changed(self, web_view, _):
        progress = web_view.get_estimated_load_progress()
        self.progress_bar.set_fraction(progress)
        self.progress_bar.set_visible(progress < 1.0)

    def on_title_changed(self, web_view, _):
        title = web_view.get_title()
        if title:
            self.title_widget.set_title(title)
            self.set_title(title)

            # Tray notification: send native GNOME notification when window is hidden
            # and the page title changes (common pattern for unread/reply indicators).
            if not self.get_visible() and title != self._last_notified_title:
                self._last_notified_title = title
                match = re.match(r"^[\(\u2022\s]*(\d+)[\)\s\u2022]", title)
                body = f"{match.group(1)} pesan baru" if match else title
                notif = Gio.Notification.new("Mistral")
                notif.set_body(body)
                notif.set_priority(Gio.NotificationPriority.HIGH)
                self.app.send_notification("mistral-reply", notif)

    def on_visibility_changed(self, *_):
        # Reset notification dedup state and withdraw any active notification
        # once the window becomes visible again.
        if self.get_visible():
            self._last_notified_title = None
            self.app.withdraw_notification("mistral-reply")

    def on_uri_changed(self, web_view, _):
        uri = web_view.get_uri()
        if uri:
            try:
                from urllib.parse import urlparse
                host = urlparse(uri).netloc
                self.title_widget.set_subtitle(host if host else "chat.mistral.ai")
            except Exception:
                self.title_widget.set_subtitle("chat.mistral.ai")

    def on_window_active_changed(self, *_):
        if self.is_active():
            self.web_view.grab_focus()

    def on_show_notification(self, web_view, notification):
        title = notification.get_title() or "Mistral"
        body = notification.get_body() or ""
        notif = Gio.Notification.new(title)
        if body:
            notif.set_body(body)
        notif.set_priority(Gio.NotificationPriority.HIGH)
        self.app.send_notification("mistral-web-notif", notif)
        return True

    def on_generation_done(self, manager, js_result):
        if not self.get_visible() or not self.is_active():
            notif = Gio.Notification.new("Mistral")
            notif.set_body("Jawaban selesai dibuat")
            notif.set_priority(Gio.NotificationPriority.HIGH)
            self.app.send_notification("mistral-generation-done", notif)

    def on_decide_policy(self, web_view, decision, decision_type):
        if decision_type == WebKit.PolicyDecisionType.RESPONSE:
            if not decision.is_mime_type_supported():
                decision.download()
                return True
        return False

    def on_load_changed(self, web_view, load_event):
        if load_event == WebKit.LoadEvent.FINISHED:
            self.progress_bar.set_visible(False)
            self.btn_back.set_sensitive(web_view.can_go_back())
            self.btn_forward.set_sensitive(web_view.can_go_forward())
            self.web_view.grab_focus()

    def on_create_popup(self, web_view, navigation_action):
        """Handle popup windows (OAuth logins) and route external links to default browser."""
        req = navigation_action.get_request()
        uri = req.get_uri() if req else None

        # External non-auth links open directly in system browser
        if uri and uri not in ("about:blank", ""):
            if not is_auth_url(uri) and not is_same_app_domain(uri):
                try:
                    Gio.AppInfo.launch_default_for_uri(uri, None)
                except Exception as e:
                    print(f"[mistral-gtk] Error launching default browser: {e}")
                return None

        popup = Adw.Window(transient_for=self, modal=False)
        popup.set_default_size(520, 680)

        popup_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        popup.set_content(popup_box)

        popup_header = Adw.HeaderBar()
        popup_header.add_css_class("flat")
        popup_title = Adw.WindowTitle(title="Login")
        popup_header.set_title_widget(popup_title)
        popup_box.append(popup_header)

        # Related view automatically inherits the parent view's network session
        popup_web_view = WebKit.WebView(
            related_view=web_view,
            user_content_manager=self.user_content_manager
        )
        popup_web_view.set_settings(self.settings)
        popup_web_view.set_vexpand(True)
        popup_web_view.set_hexpand(True)
        popup_box.append(popup_web_view)

        def on_popup_decide_policy(wv, decision, decision_type):
            if decision_type == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
                nav_action = decision.get_navigation_action()
                target_req = nav_action.get_request()
                target_uri = target_req.get_uri() if target_req else None
                if target_uri and target_uri not in ("about:blank", ""):
                    if not is_auth_url(target_uri) and not is_same_app_domain(target_uri):
                        try:
                            Gio.AppInfo.launch_default_for_uri(target_uri, None)
                        except Exception as e:
                            print(f"[mistral-gtk] Error launching default browser: {e}")
                        decision.ignore()
                        popup.close()
                        return True
            elif decision_type == WebKit.PolicyDecisionType.RESPONSE:
                if not decision.is_mime_type_supported():
                    decision.download()
                    popup.close()
                    return True
            return False

        popup_web_view.connect("decide-policy", on_popup_decide_policy)
        popup_web_view.connect(
            "notify::title",
            lambda wv, _: popup_title.set_title(wv.get_title() or "Login")
        )
        popup_web_view.connect("close", lambda _: popup.close())

        popup.present()
        return popup_web_view

    def on_permission_request(self, web_view, request):
        if isinstance(request, (WebKit.UserMediaPermissionRequest,
                                WebKit.DeviceInfoPermissionRequest,
                                WebKit.NotificationPermissionRequest)):
            request.allow()
            return True
        return False

    def on_download_started(self, session, download):
        """Route downloads to ~/Downloads with non-clobbering filenames."""
        def on_decide_destination(dl, suggested_filename):
            try:
                os.makedirs(DOWNLOAD_DIR, exist_ok=True)
                filename = suggested_filename or "download"
                dest_path = os.path.join(DOWNLOAD_DIR, filename)
                base, ext = os.path.splitext(dest_path)
                counter = 1
                while os.path.exists(dest_path):
                    dest_path = f"{base} ({counter}){ext}"
                    counter += 1

                # WebKitDownload expects a native absolute path, NOT a file:// URI
                dl.set_destination(dest_path)

                def on_finished(d):
                    print(f"[mistral-gtk] Download selesai: {dest_path}")
                    notif = Gio.Notification.new("Download Selesai")
                    notif.set_body(os.path.basename(dest_path))
                    notif.set_priority(Gio.NotificationPriority.HIGH)
                    self.app.send_notification("mistral-download-finished", notif)

                dl.connect("finished", on_finished)
                dl.connect(
                    "failed",
                    lambda d, err: print(f"[mistral-gtk] Download gagal: {err.message}")
                )
            except Exception as e:
                print(f"[mistral-gtk] Download error: {e}")
            return True

        download.connect("decide-destination", on_decide_destination)

    def on_close_request(self, _):
        # Save window dimensions
        width = self.get_width()
        height = self.get_height()
        is_max = self.is_maximized()
        save_window_state(width, height, is_max)

        # Hide to system tray instead of terminating process
        self.set_visible(False)
        return True


class MistralApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_OPEN
        )
        self.win = None
        self.tray = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        self.setup_actions()

    def do_activate(self):
        if not self.win:
            self.win = MistralWindow(self)
            self.tray = StatusNotifierTray(self, self.win)
        self.win.set_visible(True)
        self.win.present()
        self.win.web_view.grab_focus()

    def do_open(self, files, hint):
        self.do_activate()
        if files:
            for f in files:
                uri = f.get_uri()
                if uri and uri.startswith(("http://", "https://")):
                    self.win.web_view.load_uri(uri)
                    break

    def setup_actions(self):
        def add_action(name, callback):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

        add_action("zoom_in", lambda *_: self.win.zoom_in() if self.win else None)
        add_action("zoom_out", lambda *_: self.win.zoom_out() if self.win else None)
        add_action("zoom_reset", lambda *_: self.win.zoom_reset() if self.win else None)
        add_action("inspect", self.action_inspect)
        add_action("copy_url", self.action_copy_url)
        add_action("clear_cache", self.action_clear_cache)
        add_action("hide_to_tray", lambda *_: self.win.set_visible(False) if self.win else None)
        add_action("about", self.action_about)
        add_action("quit", lambda *_: self.quit())

    def action_inspect(self, *_):
        if self.win:
            inspector = self.win.web_view.get_inspector()
            inspector.show()

    def action_copy_url(self, *_):
        if self.win:
            uri = self.win.web_view.get_uri()
            if uri:
                clipboard = Gdk.Display.get_default().get_clipboard()
                clipboard.set(uri)

    def action_clear_cache(self, *_):
        if self.win and self.win.session:
            dm = self.win.session.get_website_data_manager()
            dm.clear(WebKit.WebsiteDataTypes.MEMORY_CACHE | WebKit.WebsiteDataTypes.DISK_CACHE, 0, None, None)
            self.win.web_view.reload()

    def action_about(self, *_):
        about = Adw.AboutDialog(
            application_name="Mistral GTK",
            application_icon="mistral-gtk",
            developer_name="Galyarder",
            version="0.1.0",
            copyright="\u00a9 2026 Galyarder",
            comments="Lightweight, native GTK4/Libadwaita desktop client for Mistral AI with system tray.",
            website="https://chat.mistral.ai",
            issue_url="https://github.com/muhamadgalihsaputra/mistral-gtk"
        )
        about.present(self.win)


def main():
    app = MistralApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
