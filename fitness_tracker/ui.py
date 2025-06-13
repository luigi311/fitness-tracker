import gi

gi.require_versions({"Gtk": "4.0", "Adw": "1"})
from gi.repository import Adw, Gtk, GLib

from .recorder import Recorder

Adw.init()

class FitnessAppUI(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.example.Fitness")
        self.window = None
        self.recorder: Recorder | None = None

    def do_activate(self):
        if not self.window:
            self._build()
            # wire up recorder callback
            self.recorder = Recorder(on_bpm_update=self._update_bpm)
            self.recorder.start()
        self.window.present()

    def _build(self):
        self.window = Adw.ApplicationWindow(application=self)
        self.window.set_title("Polar H10 Tracker")
        self.window.set_default_size(360, 240)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.start_btn = Gtk.Button(label="Start Recording")
        self.stop_btn = Gtk.Button(label="Stop Recording")
        self.stop_btn.set_sensitive(False)
        btn_box.append(self.start_btn)
        btn_box.append(self.stop_btn)
        vbox.append(btn_box)

        self.bpm_label = Gtk.Label(label="BPM: —")
        vbox.append(self.bpm_label)

        self.window.set_content(vbox)

        self.start_btn.connect("clicked", lambda w: self._on_start())
        self.stop_btn.connect("clicked",  lambda w: self._on_stop())

    def _on_start(self):
        self.start_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)
        self.recorder.start_recording()

    def _on_stop(self):
        self.stop_btn.set_sensitive(False)
        self.start_btn.set_sensitive(True)
        self.recorder.stop_recording()

    def _update_bpm(self, text: str):
        self.bpm_label.set_text(f"BPM: {text}")