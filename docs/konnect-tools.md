# Konnect v0.12.1 tool coverage

This table classifies every Konnect v0.12.1 tool by role, using
`plugins/circuit/skills/circuit-konnect/references/konnect-tools.json` as the
source of truth. Konnect output is advisory and never changes `kicad-cli` JSON verdicts.

## authoring

| Tool | Category | Stage | IPC | Note |
| --- | --- | --- | --- | --- |
| `add_board_outline` | pcb | layout | file |  |
| `add_board_text` | pcb | layout | file |  |
| `add_bus` | schematic | schematic | file |  |
| `add_bus_entry` | schematic | schematic | file |  |
| `add_component_annotation` | schematic | schematic | file |  |
| `add_copper_pour` | pcb | layout | optional |  |
| `add_design_rule` | project | any | file |  |
| `add_hierarchical_sheet` | schematic | schematic | file |  |
| `add_layer` | pcb | layout | file |  |
| `add_mounting_hole` | pcb | layout | optional |  |
| `add_net` | schematic | schematic | file |  |
| `add_schematic_component` | schematic | schematic | file |  |
| `add_schematic_connection` | schematic | schematic | file |  |
| `add_schematic_net_label` | schematic | schematic | file |  |
| `add_schematic_text` | schematic | schematic | file |  |
| `add_sheet_pin` | schematic | schematic | file |  |
| `add_via` | pcb | layout | file |  |
| `add_wire` | schematic | schematic | file |  |
| `add_zone` | pcb | layout | optional |  |
| `align_components` | pcb | layout | required |  |
| `assign_net_to_class` | pcb | layout | file |  |
| `auto_place_from_schematic` | schematic | schematic | file |  |
| `batch_add_bus` | schematic | schematic | file |  |
| `batch_add_junction` | schematic | schematic | file |  |
| `batch_add_no_connect` | schematic | schematic | file |  |
| `batch_add_wire` | schematic | schematic | file |  |
| `batch_connect_pins` | schematic | schematic | file |  |
| `batch_connect_to_net` | schematic | schematic | file |  |
| `batch_delete` | project | any | file |  |
| `batch_delete_no_connect` | schematic | schematic | file |  |
| `batch_delete_schematic_components` | schematic | schematic | file |  |
| `batch_delete_schematic_wire` | schematic | schematic | file |  |
| `batch_edit_schematic_components` | schematic | schematic | file |  |
| `batch_get_schematic_pin_locations` | schematic | schematic | file |  |
| `batch_place_components` | schematic | schematic | file |  |
| `batch_rotate_labels` | project | any | file |  |
| `bulk_move_schematic_components` | schematic | schematic | file |  |
| `check_schematic_overlaps` | schematic | schematic | file |  |
| `connect_passthrough` | project | any | file |  |
| `connect_pins` | schematic | schematic | file |  |
| `connect_pins_to_bus` | schematic | schematic | file |  |
| `connect_to_net` | schematic | schematic | file |  |
| `copy_routing_pattern` | project | any | file |  |
| `create_netclass` | pcb | layout | file |  |
| `create_project` | project | any | file | Project lifecycle operation used by the authoring flow. |
| `create_schematic` | schematic | schematic | file |  |
| `delete_component` | schematic | schematic | file |  |
| `delete_graphics` | project | any | file |  |
| `delete_no_connect` | schematic | schematic | file |  |
| `delete_schematic_component` | schematic | schematic | file |  |
| `delete_schematic_net_label` | schematic | schematic | file |  |
| `delete_schematic_wire` | schematic | schematic | file |  |
| `delete_sheet` | schematic | schematic | file |  |
| `delete_sheet_pin` | schematic | schematic | file |  |
| `delete_symbol` | schematic | schematic | file |  |
| `delete_trace` | pcb | layout | file |  |
| `duplicate_component` | schematic | schematic | file |  |
| `duplicate_sheet` | schematic | schematic | file |  |
| `edit_board_footprint_graphic` | pcb | layout | file |  |
| `edit_component` | schematic | schematic | file |  |
| `edit_schematic_component` | schematic | schematic | file |  |
| `edit_sheet` | schematic | schematic | file |  |
| `edit_sheet_pin` | schematic | schematic | file |  |
| `find_component` | schematic | schematic | file |  |
| `find_shorted_nets` | schematic | schematic | file |  |
| `flip_component` | pcb | layout | file |  |
| `generate_netlist` | schematic | schematic | file |  |
| `get_component_list` | pcb | layout | required |  |
| `get_component_nets` | pcb | layout | file |  |
| `get_component_pads` | pcb | layout | file |  |
| `get_net_components` | pcb | layout | file |  |
| `get_net_connections` | pcb | layout | file |  |
| `get_net_connectivity` | schematic | schematic | file |  |
| `get_nets_list` | schematic | schematic | file |  |
| `get_pad_position` | pcb | layout | file |  |
| `get_pin_connections` | pcb | layout | file |  |
| `get_pin_net_name` | pcb | layout | file |  |
| `get_project_info` | project | any | file | Project lifecycle operation used by the authoring flow. |
| `get_schematic_component` | schematic | schematic | file |  |
| `get_schematic_pin_locations` | schematic | schematic | file |  |
| `get_schematic_view` | schematic | schematic | file |  |
| `get_sheet_hierarchy` | schematic | schematic | file |  |
| `group_components` | pcb | layout | file |  |
| `import_sheet_pins` | schematic | schematic | file |  |
| `import_svg_logo` | project | any | file |  |
| `list_board_footprint_graphics` | pcb | layout | file |  |
| `list_schematic_components` | schematic | schematic | file |  |
| `list_schematic_labels` | schematic | schematic | file |  |
| `list_schematic_wires` | schematic | schematic | file |  |
| `modify_trace` | pcb | layout | file |  |
| `move_component` | pcb | layout | file |  |
| `move_connected` | project | any | file |  |
| `move_labels_by_offset` | project | any | file |  |
| `move_region` | pcb | layout | file |  |
| `move_schematic_component` | schematic | schematic | file |  |
| `move_sheet` | schematic | schematic | file |  |
| `place_component` | schematic | schematic | file |  |
| `place_component_array` | schematic | schematic | file |  |
| `renumber_sheet_pages` | schematic | schematic | file |  |
| `replace_component` | schematic | schematic | file |  |
| `rotate_component` | pcb | layout | file |  |
| `rotate_schematic_component` | schematic | schematic | file |  |
| `rotate_schematic_label` | schematic | schematic | file |  |
| `route_pad_to_pad` | pcb | layout | file |  |
| `route_trace` | pcb | layout | file |  |
| `save_project` | project | any | file | Project lifecycle operation used by the authoring flow. |
| `set_active_layer` | pcb | layout | file |  |
| `set_board_size` | pcb | layout | file |  |
| `set_component_placements` | pcb | layout | file |  |
| `set_design_rules` | pcb | layout | file |  |
| `set_layer_constraints` | pcb | layout | file |  |
| `set_schematic_page` | schematic | schematic | file |  |
| `split_wire_at_point` | schematic | schematic | file |  |
| `trace_from_point` | pcb | layout | file |  |
| `update_pcb_from_schematic` | pcb | layout | file |  |
| `validate_sheet_pins` | schematic | schematic | file |  |

## authoring_conditional

| Tool | Category | Stage | IPC | Note |
| --- | --- | --- | --- | --- |
| `add_junction` | schematic | schematic | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. |
| `add_no_connect` | schematic | schematic | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. |
| `add_power_symbol` | schematic | schematic | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. |
| `apply_specctra_ses` | pcb | layout | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. |
| `apply_template` | templates | schematic | file | alternative to create_project when a brief matches a template; brief remains authoritative |
| `check_clearance` | pcb | layout | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. |
| `check_freerouting` | pcb | layout | unknown | Requires fixture-specific prerequisites; FreeRouting tools require FreeRouting (Java/Semeru OpenJ9) — deferred. |
| `enrich_datasheets` | integration | intake | unknown | Requires network access; deferred under the pinned-library policy. |
| `export_specctra_dsn` | pcb | layout | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. |
| `get_datasheet_url` | integration | intake | unknown | Requires network access; deferred under the pinned-library policy. |
| `place_decoupling_caps` | pcb | layout | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. |
| `plan_bga_fanout` | pcb | layout | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. |
| `plan_specctra_ses_import` | pcb | layout | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. |
| `refill_zones` | pcb | layout | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. |
| `route_differential_pair` | pcb | layout | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. |
| `route_specctra_dsn` | pcb | layout | unknown | Requires matching board topology, placement, or connectivity prerequisites; run on a copy. |

## advisory

| Tool | Category | Stage | IPC | Note |
| --- | --- | --- | --- | --- |
| `annotate_schematic` | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `audit_connections` | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `audit_decoupling` | review | review | file | Advisory only; kicad-cli gate remains authoritative. |
| `audit_manufacturing` | review | review | file | Advisory only; kicad-cli gate remains authoritative. |
| `audit_power_rails` | review | review | file | Advisory only; kicad-cli gate remains authoritative. |
| `check_bom_health` | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `compare_visual_baseline` | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `estimate_cost` | review | review | file | Advisory only; kicad-cli gate remains authoritative. |
| `export_bom` | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `export_netlist_summary` | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `export_schematic_pdf` | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `export_schematic_svg` | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `find_orphan_items` | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `find_single_pin_nets` | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `fix_connectivity` | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `get_board_2d_view` | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `get_board_extents` | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `get_board_info` | pcb | layout | optional | Advisory only; kicad-cli gate remains authoritative. |
| `get_connected_items` | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `get_design_rules` | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `get_drc_violations` | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `get_layer_list` | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `get_netclasses` | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `get_schematic_layout` | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `get_template` | templates | intake | file | reference designs for circuit-brief; never a requirement source |
| `list_design_rules` | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `list_schematic_nets` | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `list_template_categories` | templates | intake | file | reference designs for circuit-brief; never a requirement source |
| `query_traces` | pcb | layout | required | Advisory only; kicad-cli gate remains authoritative. |
| `refine_placement_force_directed` | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `render_schematic_png` | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `reset_schematic_field_positions` | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `run_design_review` | review | review | file | Advisory only; kicad-cli gate remains authoritative. |
| `run_drc` | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `run_erc` | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `score_placement` | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `search_templates` | templates | intake | file | reference designs for circuit-brief; never a requirement source |
| `set_visual_baseline` | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `snapshot_project` | pcb | layout | file | Advisory only; kicad-cli gate remains authoritative. |
| `update_symbols_from_library` | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `validate_component_connections` | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |
| `validate_for_manufacturing` | review | review | file | Advisory only; kicad-cli gate remains authoritative. |
| `validate_wire_connections` | schematic | schematic | file | Advisory only; kicad-cli gate remains authoritative. |

## export_comparison

| Tool | Category | Stage | IPC | Note |
| --- | --- | --- | --- | --- |
| `export_3d` | manufacturing | manufacturing | file | Advisory export; produced artifacts are compared with authoritative exports. |
| `export_dxf` | manufacturing | manufacturing | file | Advisory export; produced artifacts are compared with authoritative exports. |
| `export_gencad` | manufacturing | manufacturing | file | Advisory export; produced artifacts are compared with authoritative exports. |
| `export_gerber` | manufacturing | manufacturing | file | Advisory export; produced artifacts are compared with authoritative exports. |
| `export_ipc2581` | manufacturing | manufacturing | file | Advisory export; produced artifacts are compared with authoritative exports. |
| `export_manufacturing_package` | manufacturing | manufacturing | file | Advisory export; produced artifacts are compared with authoritative exports. |
| `export_netlist` | schematic | schematic | file | Advisory export; authoritative kicad-cli exports remain separate. |
| `export_odb` | manufacturing | manufacturing | file | Advisory export; produced artifacts are compared with authoritative exports. |
| `export_pdf` | manufacturing | manufacturing | file | Advisory export; produced artifacts are compared with authoritative exports. |
| `export_position_file` | manufacturing | manufacturing | file | Advisory export; produced artifacts are compared with authoritative exports. |
| `export_svg` | manufacturing | manufacturing | file | Advisory export; produced artifacts are compared with authoritative exports. |

## library

| Tool | Category | Stage | IPC | Note |
| --- | --- | --- | --- | --- |
| `create_footprint` | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `create_symbol` | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `edit_footprint_pad` | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `get_footprint_info` | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `get_symbol_info` | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `list_footprint_libraries` | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `list_library_footprints` | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `list_symbol_libraries` | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `list_symbols_in_library` | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `register_footprint_library` | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `register_symbol_library` | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `repair_corrupted_footprints` | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `search_footprints` | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `search_symbols` | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `set_footprint_graphics` | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `set_footprint_metadata` | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `set_footprint_models` | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |
| `update_footprints_from_library` | library | any | file | Library operation; preserve pinned provenance and read-only source libraries. |

## lifecycle

| Tool | Category | Stage | IPC | Note |
| --- | --- | --- | --- | --- |
| `check_kicad_ui` | verification | any | unknown | Confirm api-server IPC reachability before live operations. |
| `get_active_toolsets` | lifecycle | any | file | Konnect session/toolset lifecycle; not a design verdict. |
| `get_effective_config` | config | any | file | Project configuration. |
| `get_installation_info` | lifecycle | any | file | Konnect session/toolset lifecycle; not a design verdict. |
| `get_predefined_sizes` | config | any | file | Project configuration. |
| `get_recent_calls` | lifecycle | any | file | Konnect session/toolset lifecycle; not a design verdict. |
| `list_toolboxes` | lifecycle | any | file | Konnect session/toolset lifecycle; not a design verdict. |
| `load_project_config` | config | any | file | Project configuration. |
| `load_toolset` | lifecycle | any | file | Konnect session/toolset lifecycle; not a design verdict. |
| `open_project` | project | any | unknown | Project lifecycle operation. |
| `reload_server` | lifecycle | any | file | Konnect session/toolset lifecycle; not a design verdict. |
| `save_project_config` | config | any | file | Project configuration. |
| `server_stats` | lifecycle | any | file | Konnect session/toolset lifecycle; not a design verdict. |
| `set_predefined_sizes` | config | any | file | Project configuration. |
| `unload_toolset` | lifecycle | any | file | Konnect session/toolset lifecycle; not a design verdict. |

## excluded

| Tool | Category | Stage | IPC | Note |
| --- | --- | --- | --- | --- |
| `download_jlcpcb_database` | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `get_editor_selection` | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `get_editor_state` | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `get_jlcpcb_database_stats` | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `get_jlcpcb_part` | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `launch_kicad_ui` | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `load_user_config` | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `mutate_editor_selection` | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `open_schematic_viewer` | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `rename_project` | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `resolve_cross_probe_target` | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `resolve_navigation_target` | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `save_user_config` | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `search_jlcpcb_parts` | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
| `suggest_jlcpcb_alternatives` | excluded | none | unknown | GUI-only, external database, user configuration, or destructive operation is excluded. |
