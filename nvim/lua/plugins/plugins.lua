U = require("util")

return {
	{
		"SichangHe/TeamTypeV",
		name = "teamtypev",
		config = function(plugin)
			local dir = plugin.dir .. "/nvim-plugin"
			vim.opt.rtp:append(dir)
			vim.cmd.source(dir .. "/plugin/teamtypev.lua")
		end,
		lazy = false,
	},

	{ "glacambre/firenvim", build = ":call firenvim#install(0)" },

	{
		"ibhagwan/fzf-lua",
		opts = {
			winopts = {
				fullscreen = true,
				border = false,
				preview = {
					delay = 10,
				},
			},
		},
	},

	-- NOTE: broken after treesitter update
	--[[ {
		"SichangHe/robbielyman--latex.nvim",
		branch = "fix-get-node-range-null",
		opts = {},
	}, ]]

	{
		"iamcco/markdown-preview.nvim",
		ft = "markdown",
		build = "cd app && yarn install",
		config = function()
			U.g.mkdp_auto_close = false
			U.g.mkdp_preview_options = {
				disable_filename = true,
				sync_scroll_type = "relative",
			}
			U.g.mkdp_markdown_css = U.conf_loc .. "markdown.css"
			U.g.mkdp_page_title = "${name}"
		end,
	},

	{
		"nvim-tree/nvim-web-devicons",
		opts = { default = true },
	},

	{
		"quarto-dev/quarto-nvim",
		dependencies = {
			"jmbuhr/otter.nvim",
			"nvim-treesitter/nvim-treesitter",
		},
		ft = { "quarto" },
	},

	{
		"MeanderingProgrammer/render-markdown.nvim",
		opts = {
			latex = {
				enabled = false,
			},
			win_options = {
				conceallevel = {
					-- To fix overriding latex.nvim conceal.
					rendered = 2,
				},
			},
		},
		ft = { "markdown", "quarto" },
	},

	{
		"folke/snacks.nvim",
		opts = {
			bigfile = { enabled = true },
			quickfile = { enabled = true },
		},
	},

	{
		"lervag/vimtex",
		-- Old: Help article: <https://www.ejmastnak.com/tutorials/vim-latex/pdf-reader/#refocus-nvim-macos-inverse>.
		-- Sioyek documentation: <https://sioyek-documentation.readthedocs.io/en/latest/usage.html#synctex>.
		init = function()
			-- General viewer, not "sioyek": that one launches `sioyek` and drops the column in inverse search.
			U.g.vimtex_view_method = "general"
			U.g.vimtex_view_general_viewer = "shioyek"
			-- --reuse-instance: send to the running shioyek (prefs set should_launch_new_instance), which
			-- shows the PDF in the window already open on it.
			U.g.vimtex_view_general_options = '--reuse-instance --inverse-search "'
				.. vim.v.progpath
				.. [[ --headless -c \"VimtexInverseSearch %2:%3 '%1'\"" --forward-search-file @tex --forward-search-line @line @pdf]]
		end,
		-- VimTeX cannot be lazy-loaded: <https://github.com/lervag/vimtex?tab=readme-ov-file#installation>
		lazy = false,
	},

	{
		"ojroques/nvim-osc52",
		event = "BufReadPost",
		opts = function()
			require("osc52").setup({
				tmux_passthrough = true,
			})

			-- Similar to <https://github.com/ojroques/nvim-osc52/tree/04cfaba1865ae5c53b6f887c3ca7304973824fb2?tab=readme-ov-file#advanced-usage>.
			vim.api.nvim_create_autocmd("TextYankPost", {
				callback = function()
					require("osc52").copy_register("")
				end,
			})
		end,
	},
}
