$(document).ready(() => {
    $("#logout").on("click", logout);
});

const logout = () => window.fetch("/poker/logout");