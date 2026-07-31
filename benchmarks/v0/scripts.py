"""Known compiler outputs for deterministic Planner-path acceptance."""
from __future__ import annotations


def recover_document_script(environment, task):
    del environment, task
    return (
        {
            "description": "Read primary and unavailable policy sources.",
            "operations": [
                {
                    "type": "search",
                    "op_id": "search-primary",
                    "source": "policy-kb",
                    "query": "travel reimbursement policy",
                    "limit": 5,
                    "filters_json": "{}",
                },
                {
                    "type": "search",
                    "op_id": "search-unavailable",
                    "source": "policy-failing",
                    "query": "Acme backup policy",
                    "limit": 5,
                    "filters_json": "{}",
                },
                {
                    "type": "compose",
                    "op_id": "compose-policy",
                    "inputs": [
                        {"op_id": "search-primary", "path": []},
                        {"op_id": "search-unavailable", "path": []},
                    ],
                    "strategy": "evidence_union",
                },
            ],
            "output": {"op_id": "compose-policy", "path": []},
        },
        {
            "description": "Recover with the available policy source.",
            "operations": [
                {
                    "type": "search",
                    "op_id": "search-recovery",
                    "source": "policy-kb",
                    "query": "travel reimbursement policy",
                    "limit": 5,
                    "filters_json": "{}",
                }
            ],
            "output": {"op_id": "search-recovery", "path": []},
        },
    )
