"""Tests for the shared Digest/settings display-category state."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import gui


class DisplayCategoryTests(unittest.TestCase):
    def test_empty_selection_is_preserved(self):
        self.assertEqual(
            gui._normalize_display_categories([]),
            [],
        )

    def test_digest_category_update_uses_settings_config(self):
        config = {"arxiv_categories": list(gui._DEFAULT_ARXIV_CATEGORIES)}
        with patch.object(gui, "_load_config_and_env", return_value=(config, {})):
            with patch.object(gui, "_write_config") as write_config:
                with patch.object(gui, "load_preferences", return_value={}):
                    with patch.object(gui, "save_preferences"):
                        response = gui.app.test_client().post(
                            "/preferences",
                            json={"display_categories": ["astro-ph.HE", "invalid"]},
                        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(config["arxiv_categories"], ["astro-ph.HE"])
        write_config.assert_called_once_with(config)

    def test_empty_digest_category_update_is_persisted(self):
        config = {"arxiv_categories": list(gui._DEFAULT_ARXIV_CATEGORIES)}
        with patch.object(gui, "_load_config_and_env", return_value=(config, {})):
            with patch.object(gui, "_write_config") as write_config:
                with patch.object(gui, "load_preferences", return_value={}):
                    with patch.object(gui, "save_preferences"):
                        response = gui.app.test_client().post(
                            "/preferences",
                            json={"display_categories": []},
                        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(config["arxiv_categories"], [])
        write_config.assert_called_once_with(config)


if __name__ == "__main__":
    unittest.main()
