package.path = debug.getinfo(1).source:match("@?(.*/)") .. '/?.lua;' .. package.path
local command = require('command')
local M = {}

function M.display(content, title, subtitle)
    local to_run = [[osascript -e 'display notification "]] .. content .. [["]]
    if title ~= nil then
        to_run = to_run .. [[ with title "]] .. title .. [["]]
        if subtitle ~= nil then
            to_run = to_run .. [[ subtitle "]] .. subtitle .. [["]]
        end
    end
    to_run = to_run .. [[']]
    return command.run(to_run)
end

return M
