return {
	"lewis6991/hover.nvim",
	event = "BufReadPost",
	opts = {
		providers = {
			"hover.providers.diagnostic",
			"hover.providers.lsp",
			"hover.providers.dap",
			"hover.providers.man",
			"hover.providers.dictionary",
			"hover.providers.gh",
			"hover.providers.fold_preview",
			"hover.providers.highlight",
		},
		preview_opts = {
			border = nil,
		},
		-- Whether the contents of a currently open hover window should be moved
		-- to a :h preview-window when pressing the hover keymap.
		preview_window = false,
		title = true,
	},
}
