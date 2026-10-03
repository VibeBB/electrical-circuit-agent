# Konnect v0.13.0 tool coverage

This table classifies every Konnect v0.13.0 tool by role, using
`plugins/circuit/skills/circuit-konnect/references/konnect-tools.json` as the
source of truth. Konnect output is advisory and never changes `kicad-cli` JSON verdicts.

## authoring

| Tool | Toolset | Category | Stage | IPC | Note |
| --- | --- | --- | --- | --- | --- |
| `add_board_outline` | pcb_board | pcb | layout | file |  |
| `add_board_text` | pcb_board | pcb | layout | file |  |
| `add_bus` | sch_bus | schematic | schematic | file |  |
| `add_bus_entry` | sch_bus | schematic | schematic | file |  |
| `add_component_annotation` | sch_components | schematic | schematic | file |  |
| `add_copper_pour` | pcb_routing | pcb | layout | optional |  |
| `add_design_rule` | config | project | any | file |  |
| `add_hierarchical_sheet` | sch_hierarchy | schematic | schematic | file |  |
| `add_layer` | pcb_board | pcb | layout | file |  |
| `add_mounting_hole` | pcb_board | pcb | layout | optional |  |
| `add_net` | pcb_routing | schematic | schematic | file |  |
| `add_schematic_component` | sch_components | schematic | schematic | file |  |
| `add_schematic_connection` | sch_wiring | schematic | schematic | file |  |
| `add_schematic_net_label` | sch_wiring | schematic | schematic | file |  |
| `add_schematic_text` | sch_batch | schematic | schematic | file |  |
| `add_sheet_pin` | sch_hierarchy | schematic | schematic | file |  |
| `add_via` | pcb_routing | pcb | layout | file |  |
| `add_wire` | sch_wiring | schematic | schematic | file |  |
| `add_zone` | pcb_board | pcb | layout | optional |  |
| `align_components` | pcb_components | pcb | layout | required |  |
| `assign_net_to_class` | pcb_routing | pcb | layout | file |  |
| `auto_place_from_schematic` | placement | schematic | schematic | file |  |
| `batch_add_bus` | sch_bus | schematic | schematic | file |  |
| `batch_add_junction` | sch_wiring | schematic | schematic | file |  |
| `batch_add_no_connect` | sch_wiring | schematic | schematic | file |  |
| `batch_add_wire` | sch_wiring | schematic | schematic | file |  |
| `batch_connect_pins` | sch_batch | schematic | schematic | file |  |
| `batch_connect_to_net` | sch_batch | schematic | schematic | file |  |
| `batch_delete` | sch_batch | project | any | file |  |
| `batch_delete_no_connect` | sch_wiring | schematic | schematic | file |  |
| `batch_delete_schematic_components` | sch_batch | schematic | schematic | file |  |
| `batch_delete_schematic_wire` | sch_wiring | schematic | schematic | file |  |
| `batch_edit_schematic_components` | sch_batch | schematic | schematic | file |  |
| `batch_get_schematic_pin_locations` | sch_components | schematic | schematic | file |  |
| `batch_place_components` | sch_batch | schematic | schematic | file |  |
| `batch_rotate_labels` | sch_wiring | project | any | file |  |
| `bulk_move_schematic_components` | sch_batch | schematic | schematic | file |  |
| `check_schematic_overlaps` | sch_analysis | schematic | schematic | file |  |
| `connect_passthrough` | sch_batch | project | any | file |  |
| `connect_pins` | sch_wiring | schematic | schematic | file |  |
| `connect_pins_to_bus` | sch_bus | schematic | schematic | file |  |
| `connect_to_net` | sch_wiring | schematic | schematic | file |  |
| `copy_routing_pattern` | verification | project | any | file |  |
| `create_netclass` | pcb_routing | pcb | layout | file |  |
| `create_project` | project | project | any | file | Project lifecycle operation used by the authoring flow. |
| `create_schematic` | sch_hierarchy | schematic | schematic | file |  |
| `delete_component` | pcb_components | schematic | schematic | file |  |
| `delete_graphics` | pcb_board | project | any | file |  |
| `delete_no_connect` | sch_wiring | schematic | schematic | file |  |
| `delete_schematic_component` | sch_components | schematic | schematic | file |  |
| `delete_schematic_net_label` | sch_wiring | schematic | schematic | file |  |
| `delete_schematic_wire` | sch_wiring | schematic | schematic | file |  |
| `delete_sheet` | sch_hierarchy | schematic | schematic | file |  |
| `delete_sheet_pin` | sch_hierarchy | schematic | schematic | file |  |
| `delete_symbol` | library | schematic | schematic | file |  |
| `delete_trace` | pcb_routing | pcb | layout | file |  |
| `duplicate_component` | pcb_components | schematic | schematic | file |  |
| `duplicate_sheet` | sch_hierarchy | schematic | schematic | file |  |
| `edit_board_footprint_graphic` | pcb_components | pcb | layout | file |  |
| `edit_component` | pcb_components | schematic | schematic | file |  |
| `edit_schematic_component` | sch_components | schematic | schematic | file |  |
| `edit_sheet` | sch_hierarchy | schematic | schematic | file |  |
| `edit_sheet_pin` | sch_hierarchy | schematic | schematic | file |  |
| `find_component` | pcb_components | schematic | schematic | file |  |
| `find_shorted_nets` | sch_analysis | schematic | schematic | file |  |
| `flip_component` | pcb_components | pcb | layout | file |  |
| `generate_netlist` | sch_export | schematic | schematic | file |  |
| `get_component_list` | pcb_components | pcb | layout | required |  |
| `get_component_nets` | sch_analysis | pcb | layout | file |  |
| `get_component_pads` | pcb_components | pcb | layout | file |  |
| `get_net_components` | sch_analysis | pcb | layout | file |  |
| `get_net_connections` | sch_analysis | pcb | layout | file |  |
| `get_net_connectivity` | sch_analysis | schematic | schematic | file |  |
| `get_nets_list` | pcb_routing | schematic | schematic | file |  |
| `get_pad_position` | pcb_components | pcb | layout | file |  |
| `get_pin_connections` | sch_analysis | pcb | layout | file |  |
| `get_pin_net_name` | sch_analysis | pcb | layout | file |  |
| `get_project_info` | project | project | any | file | Project lifecycle operation used by the authoring flow. |
| `get_schematic_component` | sch_components | schematic | schematic | file |  |
| `get_schematic_pin_locations` | sch_components | schematic | schematic | file |  |
| `get_schematic_view` | sch_components | schematic | schematic | file |  |
| `get_sheet_hierarchy` | sch_hierarchy | schematic | schematic | file |  |
| `group_components` | sch_components | pcb | layout | file |  |
| `import_sheet_pins` | sch_hierarchy | schematic | schematic | file |  |
| `import_svg_logo` | pcb_board | project | any | file |  |
| `list_board_footprint_graphics` | pcb_components | pcb | layout | file |  |
| `list_schematic_components` | sch_components | schematic | schematic | file |  |
| `list_schematic_labels` | sch_analysis | schematic | schematic | file |  |
| `list_schematic_wires` | sch_analysis | schematic | schematic | file |  |
| `modify_trace` | pcb_routing | pcb | layout | file |  |
| `move_component` | pcb_components | pcb | layout | file |  |
| `move_connected` | sch_components | project | any | file |  |
| `move_labels_by_offset` | sch_wiring | project | any | file |  |
| `move_region` | sch_components | pcb | layout | file |  |
| `move_schematic_component` | sch_components | schematic | schematic | file |  |
| `move_sheet` | sch_hierarchy | schematic | schematic | file |  |
| `place_component` | pcb_components | schematic | schematic | file |  |
| `place_component_array` | pcb_components | schematic | schematic | file |  |
| `renumber_sheet_pages` | sch_hierarchy | schematic | schematic | file |  |
| `replace_component` | sch_components | schematic | schematic | file |  |
| `rotate_component` | pcb_components | pcb | layout | file |  |
| `rotate_schematic_component` | sch_components | schematic | schematic | file |  |
| `rotate_schematic_label` | sch_wiring | schematic | schematic | file |  |
| `route_pad_to_pad` | pcb_routing | pcb | layout | file |  |
| `route_trace` | pcb_routing | pcb | layout | file |  |
| `save_project` | project | project | any | file | Project lifecycle operation used by the authoring flow. |
| `set_active_layer` | pcb_board | pcb | layout | file |  |
| `set_board_size` | pcb_board | pcb | layout | file |  |
| `set_component_placements` | pcb_components | pcb | layout | file |  |
| `set_design_rules` | verification | pcb | layout | file |  |
| `set_layer_constraints` | verification | pcb | layout | file |  |
| `set_placed_footprint_models` | pcb_components | pcb | layout | required | Requires live IPC with the target board open in KiCad; inspect models_revision before mutating. |
| `set_schematic_page` | sch_hierarchy | schematic | schematic | file |  |
| `split_wire_at_point` | sch_wiring | schematic | schematic | file |  |
| `trace_from_point` | sch_analysis | pcb | layout | file |  |
| `update_pcb_from_schematic` | sch_export | pcb | layout | file |  |
| `validate_sheet_pins` | sch_hierarchy | schematic | schematic | file |  |

## authoring_conditional

| Tool | Toolset | Category | Stage | IPC | Note |
| --- | --- | --- | --- | --- | --- |
| `add_junction` | sch_wiring | schematic | schematic | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. |
| `add_no_connect` | sch_wiring | schematic | schematic | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. |
| `add_power_symbol` | sch_wiring | schematic | schematic | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. |
| `apply_specctra_ses` | pcb_routing | pcb | layout | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. Depends on export_specctra_dsn, which is blocked upstream on KiCad 11 `(transform ...)` footprints (ADR-0021). |
| `apply_template` | templates | templates | schematic | file | alternative to create_project when a brief matches a template; brief remains authoritative |
| `check_clearance` | verification | pcb | layout | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. |
| `check_freerouting` | integration | pcb | layout | unknown | Requires fixture-specific prerequisites; Semeru JRE and freerouting.jar are bundled in the tools image and the check is usable (ADR-0021). |
| `enrich_datasheets` | integration | integration | intake | unknown | Requires network access; deferred under the pinned-library policy. |
| `export_specctra_dsn` | pcb_export | pcb | layout | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. Upstream export does not yet parse KiCad 11 `(transform ...)` footprint positions (ADR-0021). |
| `get_datasheet_url` | integration | integration | intake | unknown | Requires network access; deferred under the pinned-library policy. |
| `place_decoupling_caps` | placement | pcb | layout | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. |
| `plan_bga_fanout` | placement | pcb | layout | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. |
| `plan_specctra_ses_import` | pcb_routing | pcb | layout | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. Depends on export_specctra_dsn, which is blocked upstream on KiCad 11 `(transform ...)` footprints (ADR-0021). |
| `refill_zones` | pcb_export | pcb | layout | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. |
| `route_differential_pair` | pcb_routing | pcb | layout | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. |
| `route_specctra_dsn` | integration | pcb | layout | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. Depends on export_specctra_dsn, which is blocked upstream on KiCad 11 `(transform ...)` footprints (ADR-0021). |

## advisory

| Tool | Toolset | Category | Stage | IPC | Note |
| --- | --- | --- | --- | --- | --- |
| `annotate_schematic` | sch_components | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `audit_connections` | design_review | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `audit_decoupling` | design_review | review | review | file | Advisory only; kicad-cli gate remains authoritative. |
| `audit_manufacturing` | design_review | review | review | file | Advisory only; kicad-cli gate remains authoritative. |
| `audit_power_rails` | design_review | review | review | file | Advisory only; kicad-cli gate remains authoritative. |
| `check_bom_health` | design_review | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `compare_visual_baseline` | sch_export | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `estimate_cost` | manufacturing | review | review | file | Advisory only; kicad-cli gate remains authoritative. |
| `export_bom` | pcb_export | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `export_netlist_summary` | sch_export | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `export_schematic_pdf` | sch_export | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `export_schematic_svg` | sch_export | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `find_orphan_items` | sch_analysis | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `find_single_pin_nets` | sch_analysis | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `fix_connectivity` | sch_export | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `get_board_2d_view` | pcb_components | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `get_board_extents` | pcb_board | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `get_board_info` | pcb_board | pcb | layout | optional | Advisory only; kicad-cli gate remains authoritative. |
| `get_board_stackup` | pcb_board | pcb | layout | required | Advisory only; kicad-cli gate remains authoritative. Live IPC only, no file fallback. |
| `get_connected_items` | sch_analysis | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `get_design_rules` | verification | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `get_drc_violations` | pcb_export | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `get_layer_list` | pcb_board | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `get_netclasses` | pcb_routing | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `get_schematic_layout` | sch_batch | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `get_template` | templates | templates | intake | file | reference designs for circuit-brief; never a requirement source |
| `list_design_rules` | config | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `list_schematic_nets` | sch_analysis | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `list_template_categories` | templates | templates | intake | file | reference designs for circuit-brief; never a requirement source |
| `query_traces` | pcb_routing | pcb | layout | required | Advisory only; kicad-cli gate remains authoritative. |
| `refine_placement_force_directed` | placement | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `render_schematic_png` | sch_export | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `reset_schematic_field_positions` | sch_components | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `run_design_review` | design_review | review | review | file | Advisory only; kicad-cli gate remains authoritative. |
| `run_drc` | verification | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `run_erc` | sch_export | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `score_placement` | placement | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `search_templates` | templates | templates | intake | file | reference designs for circuit-brief; never a requirement source |
| `set_visual_baseline` | sch_export | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `snapshot_project` | project | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `update_symbols_from_library` | sch_components | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `validate_component_connections` | sch_batch | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `validate_for_manufacturing` | manufacturing | review | review | file | Advisory only; kicad-cli gate remains authoritative. |
| `validate_wire_connections` | sch_batch | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |

## export_comparison

| Tool | Toolset | Category | Stage | IPC | Note |
| --- | --- | --- | --- | --- | --- |
| `export_3d` | pcb_export | manufacturing | manufacturing | file | Advisory export; produced artifacts are compared with authoritative exports. |
| `export_dxf` | pcb_export | manufacturing | manufacturing | file | Advisory export; produced artifacts are compared with authoritative exports. |
| `export_gencad` | pcb_export | manufacturing | manufacturing | file | Advisory export; produced artifacts are compared with authoritative exports. |
| `export_gerber` | pcb_export | manufacturing | manufacturing | file | Advisory export; produced artifacts are compared with authoritative exports. |
| `export_ipc2581` | pcb_export | manufacturing | manufacturing | file | Advisory export; produced artifacts are compared with authoritative exports. |
| `export_manufacturing_package` | manufacturing | manufacturing | manufacturing | file | Advisory export; produced artifacts are compared with authoritative exports. |
| `export_netlist` | pcb_export | schematic | schematic | file | Advisory export; authoritative kicad-cli exports remain separate. |
| `export_odb` | pcb_export | manufacturing | manufacturing | file | Advisory export; produced artifacts are compared with authoritative exports. |
| `export_pdf` | pcb_export | manufacturing | manufacturing | file | Advisory export; produced artifacts are compared with authoritative exports. |
| `export_position_file` | pcb_export | manufacturing | manufacturing | file | Advisory export; produced artifacts are compared with authoritative exports. |
| `export_svg` | pcb_export | manufacturing | manufacturing | file | Advisory export; produced artifacts are compared with authoritative exports. |

## library

| Tool | Toolset | Category | Stage | IPC | Note |
| --- | --- | --- | --- | --- | --- |
| `create_footprint` | library | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `create_symbol` | library | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `edit_footprint_pad` | library | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `get_footprint_info` | library | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `get_symbol_info` | library | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `list_footprint_libraries` | library | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `list_library_footprints` | library | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `list_symbol_libraries` | library | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `list_symbols_in_library` | library | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `register_footprint_library` | library | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `register_symbol_library` | library | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `repair_corrupted_footprints` | pcb_components | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `search_footprints` | library | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `search_symbols` | library | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `set_footprint_graphics` | library | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `set_footprint_metadata` | library | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `set_footprint_models` | library | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `update_footprints_from_library` | pcb_components | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |

## lifecycle

| Tool | Toolset | Category | Stage | IPC | Note |
| --- | --- | --- | --- | --- | --- |
| `check_kicad_ui` | verification | verification | any | unknown | Confirm api-server IPC reachability before live operations. |
| `get_active_toolsets` | core | lifecycle | any | file | Konnect session/toolset lifecycle; not a design verdict. |
| `get_effective_config` | config | config | any | file | Project configuration. |
| `get_installation_info` | core | lifecycle | any | file | Konnect session/toolset lifecycle; not a design verdict. |
| `get_predefined_sizes` | verification | config | any | file | Project configuration. |
| `get_recent_calls` | core | lifecycle | any | file | Konnect session/toolset lifecycle; not a design verdict. |
| `list_toolboxes` | core | lifecycle | any | file | Konnect session/toolset lifecycle; not a design verdict. |
| `load_project_config` | config | config | any | file | Project configuration. |
| `load_toolset` | core | lifecycle | any | file | Konnect session/toolset lifecycle; not a design verdict. |
| `open_project` | project | project | any | unknown | Project lifecycle operation. |
| `reload_server` | core | lifecycle | any | file | Konnect session/toolset lifecycle; not a design verdict. |
| `save_project_config` | config | config | any | file | Project configuration. |
| `server_stats` | core | lifecycle | any | file | Konnect session/toolset lifecycle; not a design verdict. |
| `set_predefined_sizes` | verification | config | any | file | Project configuration. |
| `unload_toolset` | core | lifecycle | any | file | Konnect session/toolset lifecycle; not a design verdict. |

## excluded

| Tool | Toolset | Category | Stage | IPC | Note |
| --- | --- | --- | --- | --- | --- |
| `download_jlcpcb_database` | integration | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `get_editor_selection` | editor_navigation | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `get_editor_state` | editor_navigation | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `get_jlcpcb_database_stats` | integration | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `get_jlcpcb_part` | integration | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `launch_kicad_ui` | verification | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `load_user_config` | config | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `mutate_editor_selection` | editor_navigation | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `open_schematic_viewer` | project | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `rename_project` | project | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `resolve_cross_probe_target` | editor_navigation | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `resolve_navigation_target` | editor_navigation | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `save_user_config` | config | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `search_jlcpcb_parts` | integration | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `suggest_jlcpcb_alternatives` | integration | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
