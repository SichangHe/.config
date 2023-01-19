return function(use)
    use {
        'numToStr/Comment.nvim',
        event = 'CmdLineEnter',
        config = function() require('Comment').setup {} end,
    }

    use {
        'ibhagwan/fzf-lua',
        event = 'CmdLineEnter',
        requires = { 'nvim-tree/nvim-web-devicons' },
        config = function()
            require('fzf-lua').setup {
                fullscreen = true,
                previewers = {
                    builtin = {
                        extensions = {
                            ["png"] = { "viu" },
                            ["jpg"] = { "viu" },
                        },
                    },
                },
            }
        end,
    }

    use {
        'windwp/nvim-autopairs',
        event = 'InsertEnter',
        config = function() require('nvim-autopairs').setup {} end,
    }

    use {
        'nvim-tree/nvim-web-devicons',
        event = 'CmdLineEnter',
        config = function()
            require('nvim-web-devicons').setup {
                default = true
            }
        end,
    }

    use {
        'machakann/vim-swap',
        event = 'InsertEnter',
    }
end
