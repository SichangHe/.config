U = require("util")

return {
	{
		"saghen/blink.cmp",
		opts = function(_, opts)
			opts.sources.default = table.insert(opts.sources.default, 1, {
				"crates",
				"lsp",
				"vimtex",
				"snippets",
				"buffer",
				"copilot",
				"codeium",
				"tabnine",
			})
			opts.signature = { enabled = true }
			-- Disable LazyVim <C-k> in insert mode.
			local keys = require("lazyvim.plugins.lsp.keymaps").get()
			keys[#keys + 1] = { "<C-k>", mode = { "i", "s" }, false }
			opts.keymap = U.deep_extend(opts.keymap, {
				["<Tab>"] = {
					function(cmp)
						if cmp.is_visible() then
							U.break_undo()
							cmp.accept()
							return true
						end
						return false
					end,
					"fallback",
				},
				["<C-j>"] = {
					function(cmp)
						if not cmp.is_visible() then
							cmp.show()
						end
						cmp.select_next()
						return true
					end,
				},
				["<C-k>"] = {
					function(cmp)
						if not cmp.is_visible() then
							cmp.show()
						end
						cmp.select_prev()
						return true
					end,
				},
			})
		end,
	},

	{
		"zbirenbaum/copilot.lua",
		opts = {
			filetypes = {
				["*"] = true,
			},
		},
	},

	{
		"rafamadriz/friendly-snippets",
		config = function()
			require("luasnip.loaders.from_vscode").lazy_load({})
			require("luasnip.loaders.from_vscode").lazy_load({
				paths = { U.expand("~/.config/Code/User/snippets") },
			})
		end,
	},
}
