{
  "mode": "inventory",
  "policy": {
    "blockers": 151,
    "digest": "sha256:0781474294cdfb520df55ba9feac384ac388579b7c71dd9af86c7b25d39130b7",
    "errors": 0,
    "findings": 218,
    "warnings": 67,
    "work_queue": {
      "blocking": 39,
      "by_action": {
        "add_requirement_evidence": 19,
        "dispose_source_risk": 26,
        "repair_causality": 19,
        "resolve_owner": 11,
        "rewire_default": 9
      },
      "by_owner_unit": {
        "M3-01B": 65,
        "M3-02A": 11,
        "M3-02B": 2,
        "M3-03": 6
      },
      "by_priority": {
        "p0": 20,
        "p1": 19,
        "p3": 45
      },
      "items": 84
    }
  },
  "receipt_digest": "sha256:689f436ebf1b39c6c918576f2b852aa8e85f04dc8082e075fe48ec6e0fe3a8d2",
  "release_ready": false,
  "revision": "7ff45a7c52539c920539d398a9469c3ef7c7aeb8",
  "schema": "zyra.state-owner-reachability-evidence-audit/v1",
  "sections": {
    "default_reachability": {
      "digest": "sha256:9f95c12b97363fa60ce517b61ff5aeb840b19aff26c6abcf830ee7d82144b635",
      "findings": 30,
      "metrics": {
        "default_entry_count": 11,
        "edges": 214876,
        "entry_count": 11,
        "graph_digest": "sha256:d82572a110b2c52459dad67b6dac180f0332f4a1526927689fa4290a5ffc7b3f",
        "nodes": 85024,
        "reachable_entry_count": 3,
        "reachable_node_count": 15717,
        "rule_enabled": true,
        "schema": "zyra.default-entry-reachability/v1",
        "strongly_connected_components": 84562,
        "surface_counts": {
          "api": 1,
          "cli": 1,
          "web": 2,
          "worker": 7
        },
        "unreachable_entry_count": 8
      },
      "valid": false
    },
    "effective_lines_parent": {
      "digest": "sha256:f9893509209c453c97ed98f369e27c033234f199a617447df74850f1e6a42bca",
      "findings": 0,
      "metrics": {
        "audit_id": "parent",
        "baseline": "4b6d0d1d81332886385adcd32ff6205f6403d0f0",
        "bucket_effective": {
          "adapter_only": 0,
          "data": 0,
          "docs": 0,
          "production": 13009,
          "test": 0
        },
        "bucket_raw": {
          "adapter_only": 14,
          "data": 108,
          "docs": 53804,
          "production": 16810,
          "test": 1608
        },
        "data_as_code": 0,
        "effective_by_language": {
          "python": 12981,
          "typescript": 28
        },
        "effective_production": 13009,
        "exclusions": {
          "adapter_only": 13,
          "annotation_only": 2,
          "blank": 946,
          "comment": 18,
          "data": 108,
          "docs": 53804,
          "docstring_or_literal": 55,
          "import": 509,
          "interface_or_type": 1022,
          "pass_only": 2,
          "protocol_or_interface": 28,
          "schema_dto_field": 436,
          "schema_dto_header": 57,
          "signature_continuation": 727,
          "test": 1608
        },
        "files_over_20_percent": 0,
        "head": "7ff45a7c52539c920539d398a9469c3ef7c7aeb8",
        "high_exclusion_files": 33,
        "interval_digest": "sha256:f69a0297beb83ea4ffdd3902da9980274378d8f2200dcbcb523caba5830d5c24",
        "large_files": 20,
        "margin": 4009,
        "minimum": 9000,
        "passed": true,
        "raw_added": 72344,
        "raw_deleted": 2845,
        "rule_enabled": true
      },
      "valid": true
    },
    "effective_lines_slice": {
      "digest": "sha256:9e1632f3f8c5d5870f266f4e362deec1c499a96d253f842f44098be2a1bda33e",
      "findings": 0,
      "metrics": {
        "audit_id": "slice",
        "baseline": "9f2bfe85bc8324e2531e0ba636cc4410bda9334e",
        "bucket_effective": {
          "adapter_only": 0,
          "data": 0,
          "docs": 0,
          "production": 6310,
          "test": 0
        },
        "bucket_raw": {
          "adapter_only": 14,
          "data": 65,
          "docs": 268,
          "production": 8724,
          "test": 989
        },
        "data_as_code": 0,
        "effective_by_language": {
          "python": 6303,
          "typescript": 7
        },
        "effective_production": 6310,
        "exclusions": {
          "adapter_only": 13,
          "annotation_only": 1,
          "blank": 458,
          "comment": 11,
          "data": 65,
          "docs": 268,
          "docstring_or_literal": 3,
          "import": 258,
          "interface_or_type": 1022,
          "pass_only": 2,
          "protocol_or_interface": 19,
          "schema_dto_field": 182,
          "schema_dto_header": 25,
          "signature_continuation": 434,
          "test": 989
        },
        "files_over_20_percent": 0,
        "head": "7ff45a7c52539c920539d398a9469c3ef7c7aeb8",
        "high_exclusion_files": 16,
        "interval_digest": "sha256:5bf5f87a568ff1f684357057041782e0c22b8bb92d301f7a9ca039d41bbc3fe5",
        "large_files": 9,
        "margin": 1810,
        "minimum": 4500,
        "passed": true,
        "raw_added": 10060,
        "raw_deleted": 2704,
        "rule_enabled": true
      },
      "valid": true
    },
    "event_mutation_causality": {
      "digest": "sha256:d5b4c5ae8502577fa2bdb0666d1007bacb11a3d67386ccbb4598484e73058701",
      "findings": 41,
      "metrics": {
        "declared_links": 11,
        "domains_with_valid_link": 11,
        "effect_kinds": {
          "artifact": 1,
          "compact": 1,
          "permission": 1,
          "placement": 1,
          "recovery": 2,
          "route": 1,
          "state_mutation": 3,
          "tool": 1
        },
        "event_found": 11,
        "invalid_links": 0,
        "mutation_signal": 11,
        "rule_enabled": true,
        "semantic_effect": 11,
        "valid_links": 11
      },
      "valid": true
    },
    "python_graph": {
      "digest": "sha256:4df674bd81b1d51be40b7134afd0f3f9ba4ed962cb5bd3d06878b9fbecabb6bb",
      "findings": 0,
      "metrics": {
        "assignments": 80373,
        "calls": 166312,
        "edges": 96595,
        "events": 267,
        "files": 997,
        "imports": 21322,
        "nodes": 40204,
        "owner_claims": 87,
        "parse_errors": 0,
        "production_files": 845,
        "routes": 0,
        "symbols": 23325,
        "write_calls": 14829
      },
      "valid": true
    },
    "requirement_runtime_evidence": {
      "digest": "sha256:151ccafba170f82fd43936d9b0e8296c569d5343761694662238a68ff7f5f9f3",
      "findings": 59,
      "metrics": {
        "evidence_digest": "sha256:a81fb8e6acb7a81ad92c17f016f22a28054f441015c93f0a46cdbdfff75aee2f",
        "invalid_count": 19,
        "m3_owner_counts": {
          "M3-02A": 11,
          "M3-02B": 2,
          "M3-03": 6
        },
        "requirement_count": 19,
        "rule_enabled": true,
        "status_counts": {
          "partial": 4,
          "verified": 15
        },
        "valid_count": 0,
        "verified_count": 15
      },
      "valid": false
    },
    "script_graph": {
      "digest": "sha256:23e885247c63c592e084a3aec80a88f09c435f2b2a2cac4cee91d4acedea50b1",
      "findings": 0,
      "metrics": {
        "assignments": 57377,
        "calls": 261336,
        "edges": 118257,
        "entry_calls": 1042,
        "events": 2278,
        "files": 846,
        "imports": 3371,
        "nodes": 44809,
        "owner_claims": 25,
        "parse_errors": 0,
        "production_files": 742,
        "symbols": 30806,
        "write_calls": 23506
      },
      "valid": true
    },
    "source_risk_bridge": {
      "digest": "sha256:6345c0ab1a865e72522e87e2fd3000cd1105aab3e4a1d23eebdc4421c1f9f263",
      "findings": 26,
      "metrics": {
        "blocking_risk_records": 0,
        "bridge_digest": "sha256:8e3de04936376d523db11452343e20104b08dab67e0fd83a22b34e0253e846f3",
        "input_valid": true,
        "langgraph_forbidden_hits": 21,
        "opaque_runtime_hits": 0,
        "repository_manifest_items": 4097,
        "risk_categories": {
          "dynamic_download": 18,
          "opaque_bundle": 7,
          "semantic_port": 1
        },
        "risk_records": 26,
        "rule_enabled": true,
        "similarity_pairs": 1,
        "source_receipt_digest": "sha256:9258f3b605f599f93967a373fe584083a268830b4d797bd41424ddd9e3a8de18",
        "source_revision": "7ff45a7c52539c920539d398a9469c3ef7c7aeb8",
        "work_queue_items": 235
      },
      "valid": true
    },
    "state_catalog": {
      "digest": "sha256:8e9b6637597c6567d6c33e77dc0858ae9a36575b8cc13a7d857064f235598072",
      "findings": 0,
      "metrics": {
        "catalog_digest": "sha256:575f4d67543f97e59b0910abac82b77bd6a6da5a0a7662dc45af363002cb4e6f",
        "catalog_path": "packages/evaluation/zyra_evaluation/data/state_owner_evidence_catalog.json",
        "default_entry_count": 11,
        "event_mutation_count": 11,
        "matrix_requirement_count": 19,
        "owner_count": 11,
        "raw_digest": "sha256:d18f85bd8be8ff8391d24b3418184e93b9134b9fe5c11e437bbfa0de6758951f",
        "required_domain_count": 11,
        "required_requirement_count": 19,
        "requirement_count": 19,
        "rule_enabled": true,
        "schema": "zyra.state-owner-evidence-catalog/v1"
      },
      "valid": true
    },
    "state_ownership": {
      "digest": "sha256:560ebf8091d9af5e4259a3fd3e2a8ae9db72449ac8439a49a965dfe335252ee2",
      "findings": 62,
      "metrics": {
        "cache_count": 2,
        "canonical_claim_count": 0,
        "domain_count": 11,
        "invalid_reference_count": 0,
        "owner_count": 11,
        "projection_count": 7,
        "reference_count": 67,
        "role_counts": {
          "cache": 2,
          "checkpoint": 11,
          "fallback": 1,
          "owner": 11,
          "projection": 7,
          "recovery": 11,
          "store": 11,
          "writer": 13
        },
        "rule_enabled": true,
        "store_count": 11,
        "valid_reference_count": 67,
        "writer_count": 13
      },
      "valid": false
    }
  },
  "slice_id": "M3-S01A-02",
  "valid": true
}
