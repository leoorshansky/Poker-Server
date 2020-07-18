$(document).ready(() => {
    $("#logout").on("click", logout);
    getAvatar();
});

const logout = () => window.fetch("/poker/logout", {credentials: 'include'}).then(() => window.location.assign("/poker/"));
const getAvatar = async () => {
    const res = await window.fetch("/poker/avatar", {credentials: 'include'});
    if (res.ok){
        $("#avatar").prop("src", await res.text())
        return
    }
    $("#avatar").css("display", "none");
};