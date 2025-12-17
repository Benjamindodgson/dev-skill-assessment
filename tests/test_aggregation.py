import unittest

from devskill import azure_metrics, cli, metrics


class AggregationTests(unittest.TestCase):
    def test_merge_repo_payloads_accumulates_across_repos_and_keeps_azure(self):
        since_iso = "2024-01-01T00:00:00Z"
        until_iso = "2024-01-10T00:00:00Z"

        pr_base = {
            "state": "MERGED",
            "isDraft": False,
            "createdAt": "2024-01-02T00:00:00Z",
            "mergedAt": "2024-01-03T00:00:00Z",
            "additions": 10,
            "deletions": 5,
            "reviews": {"nodes": []},
        }
        repo1 = {
            "owner": "o1",
            "repo": "r1",
            "since": since_iso,
            "until": until_iso,
            "pull_requests": [
                {**pr_base, "number": 1, "author": {"login": "devA"}},
            ],
            "commits": [
                {"author": {"login": "devA"}, "commit": {"author": {"email": "devA@example.com"}}},
            ],
            "ado": {},
            "ado_bug_items": [],
            "ado_pr_bugs": {},
            "azure": {},
        }
        repo2 = {
            "owner": "o2",
            "repo": "r2",
            "since": since_iso,
            "until": until_iso,
            "pull_requests": [
                {**pr_base, "number": 2, "author": {"login": "devA"}},
            ],
            "commits": [
                {"author": {"login": "devA"}, "commit": {"author": {"email": "devA@example.com"}}},
            ],
            "ado": {},
            "ado_bug_items": [],
            "ado_pr_bugs": {},
            "azure": {},
        }

        azure_data = {
            "since": since_iso,
            "until": until_iso,
            "ready_states": ["Ready"],
            "resolved_states": ["Done"],
            "qa_failed_states": ["QA Failed"],
            "work_items": [
                {
                    "id": 1,
                    "fields": {
                        "System.CreatedDate": since_iso,
                        "System.State": "Ready",
                        "System.IterationPath": "Sprint1",
                        "Microsoft.VSTS.Scheduling.StoryPoints": 3,
                    },
                    "updates": [
                        {
                            "revisedDate": "2024-01-03T00:00:00Z",
                            "fields": {"System.State": {"oldValue": "Ready", "newValue": "Done"}},
                        }
                    ],
                }
            ],
            "iterations": [
                {
                    "id": "it1",
                    "path": "Sprint1",
                    "name": "Sprint1",
                    "attributes": {"startDate": since_iso, "finishDate": until_iso},
                }
            ],
        }

        merged = cli._merge_repo_payloads(
            repos_data=[repo1, repo2],
            since_iso=since_iso,
            until_iso=until_iso,
            azure_data=azure_data,
        )

        self.assertEqual(len(merged["pull_requests"]), 2)
        self.assertEqual(len(merged["commits"]), 2)
        self.assertEqual(len(merged["repos"]), 2)
        self.assertEqual(merged["azure"].get("since"), since_iso)
        self.assertSetEqual({pr["repo"] for pr in merged["pull_requests"]}, {"r1", "r2"})

        scores = metrics.compute_scores(
            data=merged,
            owner="multiple",
            repo="aggregate",
            since_iso=since_iso,
            until_iso=until_iso,
            config={},
        )
        self.assertEqual(scores["team"]["count"], 1)
        self.assertEqual(scores["developers"][0]["delivery.merged_prs"], 2.0)

        azure_scores = azure_metrics.compute_scores(
            azure_data=merged.get("azure", {}),
            config={},
        )
        self.assertEqual(azure_scores["team"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
