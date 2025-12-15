local disable_lang = {
	latex = true, -- Using VimTex instead.
}

return {
	{
		"nvim-treesitter/nvim-treesitter",
		lazy = false,
		build = ":TSUpdate",
		dependencies = {
			"RRethy/nvim-treesitter-endwise",
			"windwp/nvim-ts-autotag",
			"hiphish/rainbow-delimiters.nvim",
		},
		opts = {
			highlight = {
				enable = true,
				disable = function(lang, bufnr)
					_ = bufnr
					return disable_lang[lang]
				end,
			},
			incremental_selection = {
				enable = true,
				keymaps = {
					init_selection = "gnn",
					node_incremental = "grn",
					scope_incremental = "grc",
					node_decremental = "grm",
				},
			},
			auto_install = true,
			autotag = {
				enable = true,
			},
			endwise = {
				enable = true,
			},
		},
	},

	{
		"hiphish/rainbow-delimiters.nvim",
		config = function()
			require("rainbow-delimiters.setup").setup({
				strategy = {
					latex = nil,
				},
				highlight = {
					"rainbowcol1",
					"rainbowcol2",
					"rainbowcol3",
					"rainbowcol4",
					"rainbowcol5",
					"rainbowcol6",
				},
			})
		end,
		lazy = true,
	},
}
