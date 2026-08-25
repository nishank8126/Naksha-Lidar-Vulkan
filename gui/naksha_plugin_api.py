class NakshaPlugin:
    def on_load(self, app_window):
        """Called once when plugin is loaded."""
        self.app_window = app_window

    def get_ribbon_button(self):
        """
        Optional. Return a dict like:
        {"label": "My Tool", "emoji": "🔧", "section": "My Tools", "callback": self.run}
        to add a button to the Plugins ribbon.
        """
        return None

    def on_unload(self):
        """Called before plugin is removed."""
        pass