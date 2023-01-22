return function(use)
    use {
        'numToStr/Comment.nvim',
        event = 'VimEnter',
        config = function() require('Comment').setup {} end,
    }

    use {
        'saecki/crates.nvim',
        event = { 'BufRead Cargo.toml' },
        requires = { 'hrsh7th/nvim-cmp', 'nvim-lua/plenary.nvim' },
        config = function()
            require('crates').setup {}
            require('cmp').setup.buffer {
                sources = {
                    { name = 'crates' },
                },
            }
        end,
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
        'machakann/vim-swap',
        event = 'InsertEnter',
    }
end