return {
	-- Email through the `himalaya` CLI; reads `~/.config/himalaya/config.toml`.
	{
		"pimalaya/himalaya-vim",
		cmd = "Himalaya",
		keys = { { "<leader>oe", "<cmd>Himalaya<cr>", desc = "Email inbox" } },
	},

	-- Omnigent agents; needs the Omnigent server on the same machine.
	{
		"SichangHe/nvim_omnigent",
		event = "VeryLazy",
		opts = {},
	},
}
