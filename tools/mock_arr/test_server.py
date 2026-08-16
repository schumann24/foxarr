from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent))
import server  # noqa: E402


class MockSonarrContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_state = copy.deepcopy(server.state)
        self.original_state_file = server.STATE_FILE
        self.original_role = server.ROLE
        server.ROLE = "sonarr"
        server.state.clear()
        server.state.update(
            {
                "movies": {},
                "series": {},
                "episodes": {},
                "commands": {},
                "seq": 1000,
            }
        )
        server.STATE_FILE = None

    def tearDown(self) -> None:
        server.state.clear()
        server.state.update(self.original_state)
        server.STATE_FILE = self.original_state_file
        server.ROLE = self.original_role

    def test_selected_seasons_control_series_and_episode_state(self) -> None:
        series = server._lookup_from_term("tvdb:121361")
        series["id"] = 1001
        server.state["series"]["1001"] = series
        server._episodes_for(series["id"], series["seasons"])

        server._apply_series_body(
            series,
            {
                "seasons": [
                    {"seasonNumber": 1, "monitored": True},
                    {"seasonNumber": 2, "monitored": False},
                ],
                "qualityProfileId": 1,
                "rootFolderPath": "/tv",
                "addOptions": {"searchForMissingEpisodes": False},
            },
        )

        monitored = {s["seasonNumber"]: s["monitored"] for s in series["seasons"]}
        self.assertEqual(monitored, {1: True, 2: False})
        episodes = list(server.state["episodes"].values())
        self.assertEqual({e["seasonNumber"] for e in episodes}, {1})
        self.assertEqual(len(episodes), 3)

    def test_episode_state_is_json_serializable_and_restored_as_tuple_index(self) -> None:
        series = server._lookup_from_term("tvdb:121361")
        series["id"] = 1001
        server.state["series"]["1001"] = series
        server._episodes_for(series["id"], [{"seasonNumber": 1, "monitored": True}])

        with tempfile.TemporaryDirectory() as directory:
            state_file = os.path.join(directory, "state.json")
            server.STATE_FILE = state_file
            server._save_state()

            with open(state_file, encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertIsInstance(saved["episodes"], list)
            self.assertEqual(len(saved["episodes"]), 3)

            server.state["episodes"] = {
                (int(e["seriesId"]), int(e["seasonNumber"]), int(e["episodeNumber"])): e
                for e in saved["episodes"]
            }
            self.assertIn((1001, 1, 1), server.state["episodes"])


if __name__ == "__main__":
    unittest.main()
