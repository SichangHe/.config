return function(use)
    use {
        'sindrets/diffview.nvim',
        event = 'CmdLineEnter',
        requires = {
            'nvim-tree/nvim-web-devicons',
            'nvim-lua/plenary.nvim',
        },
    }

    use {
        'lewis6991/gitsigns.nvim',
        config = function()
            require('gitsigns').setup()
        end,
    }

    use {
        'tpope/vim-fugitive',
        event = 'CmdLineEnter',
    }
end
