U = require('util')
local vscode_exists = U.fn.exists('g:vscode') == 1
local loaded = false

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
        if loaded then
            use('config.autocmds')
            use('config.keymaps')
            use('config.options')
        else
            use('config.lazy')
        end
    end

    loaded = true
end

AllConfig()
