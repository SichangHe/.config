return {
	{
		"nvim-treesitter/nvim-treesitter",
		build = ":TSUpdate",
		dependencies = {
			"RRethy/nvim-treesitter-endwise",
			"windwp/nvim-ts-autotag",
			"hiphish/rainbow-delimiters.nvim",
		},
		config = function()
			local disable_lang = {
				latex = true, -- Using VimTex instead.
			}
			require("nvim-treesitter.configs").setup({
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
				ensure_installed = {
					"bash",
					"c",
					"elixir",
					"erlang",
					"fish",
					"heex",
					"javascript",
					"jsonc",
					"julia",
					"latex",
					"lua",
					"markdown",
					"markdown_inline",
					"python",
					"ruby",
					"rust",
					"typescript",
					"vim",
				},
				auto_install = true,
				autotag = {
					enable = true,
				},
				endwise = {
					enable = true,
				},
			})
		end,
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
