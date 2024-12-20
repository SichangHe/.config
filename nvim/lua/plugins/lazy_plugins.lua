U = require("util")

return {
	{
		"numToStr/Comment.nvim",
		event = "VeryLazy",
		config = true,
	},

	{
		"ziontee113/icon-picker.nvim",
		event = "InsertEnter",
		dependencies = { "stevearc/dressing.nvim" },
		opts = {
			disable_legacy_commands = true,
		},
	},
	{
		"adelarsq/image_preview.nvim",
		event = "VeryLazy",
		config = true,
	},

	{
		"mikesmithgh/kitty-scrollback.nvim",
		cmd = { "KittyScrollbackGenerateKittens", "KittyScrollbackCheckHealth" },
		event = { "User KittyScrollbackLaunch" },
		config = true,
		build = ":KittyScrollbackGenerateKittens",
	},

	{
		"Wansmer/sibling-swap.nvim",
		dependencies = { "nvim-treesitter/nvim-treesitter" },
		event = { "InsertEnter" },
		config = function()
			local swap = require("sibling-swap")
			swap.setup({
				use_default_keymaps = false,
			})
			U.key("i", "<C-,>", swap.swap_with_left)
			U.key("i", "<C-.>", swap.swap_with_right)
			U.key("i", "<C-S-,>", swap.swap_with_left_with_opp)
			U.key("i", "<C-S-.>", swap.swap_with_right_with_opp)
		end,
	},

	{
		"altermo/ultimate-autopair.nvim",
		event = { "InsertEnter", "CmdlineEnter" },
		-- <https://github.com/altermo/ultimate-autopair.nvim/blob/v0.6/Q%26A.md>
		opts = {
			extensions = {
				filetype = {
					nft = { "javascript" }, --Disable because broken.
				},
				cond = {
					---Disable in replace mode.
					cond = function(fn)
						return fn.get_mode() ~= "R"
					end,
				},
			},
		},
	},

	{
		"linux-cultist/venv-selector.nvim",
		branch = "regexp", -- Use this branch for the new version
		opts = {
			settings = {
				options = {
					notify_user_on_venv_activation = true,
				},
			},
		},
		cmd = "VenvSelect",
		ft = "python",
		keys = { { "<leader>cv", "<cmd>:VenvSelect<cr>", desc = "Select VirtualEnv", ft = "python" } },
	},
}
