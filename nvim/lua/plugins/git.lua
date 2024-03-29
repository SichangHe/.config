return {
    {
        'sindrets/diffview.nvim',
        event = 'CmdLineEnter',
        dependencies = {
            'nvim-tree/nvim-web-devicons',
            'nvim-lua/plenary.nvim',
        },
    },

    {
        'lewis6991/gitsigns.nvim',
        opts = {
            signcolumn = false,
            numhl = true,
        },
    },

    {
        'tpope/vim-fugitive',
        event = 'CmdLineEnter',
    },
}
