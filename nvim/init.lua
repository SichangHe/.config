U = require('util')
local vscode_exists = U.fn.exists('g:vscode') == 1

function AllConfig()
    U = U.use('util')
    local use = U.use
    GeneralOptions = use('general_options')
    GeneralKeymaps = use('general_keymaps')
    GeneralOptions.set()
    GeneralKeymaps.set()

    if vscode_exists then
        -- Running inside vscode
    else
        use('config.lazy')

        Options = use('options')
        Keymaps = use('keymaps')
        Au = use('autocmd')

        Options.set()
        Keymaps.set()
        Au.set()
    end
end

AllConfig()
