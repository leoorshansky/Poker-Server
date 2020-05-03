$(document).ready(() => {
    $("#logout").addEventListener("click", logout);
});

const logout = () => window.fetch("/poker/logout");