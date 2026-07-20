// Public config

// @Deprecated, will be replaced with antiCaptchaPredefinedApiKey
var antiCapthaPredefinedApiKey = '';
var antiCaptchaPredefinedApiKey = antiCapthaPredefinedApiKey;

var defaultConfig = {
    // settings
    enable: true,
    account_key: antiCaptchaPredefinedApiKey,
    auto_submit_form: true,
    play_sounds: false,
    delay_onready_callback: false,

    where_solve_list: [], // i.e. ['example.com', 'www.ladysproblems.com']
    where_solve_white_list_type: false, // true -- considered as white list, false -- black list

    solve_recaptcha2: true,
    solve_recaptcha3: true,
    recaptcha3_score: 0.3,
    solve_invisible_recaptcha: true,
    solve_funcaptcha: true,
    solve_geetest: true,
    solve_hcaptcha: true,
    solve_turnstile: true,
    use_predefined_image_captcha_marks: true,
    reenable_contextmenu: false,

    solve_proxy_on_tasks: false,
    user_proxy_protocol: 'HTTP',
    user_proxy_server: '',
    user_proxy_port: '',
    user_proxy_login: '',
    user_proxy_password: '',

    use_recaptcha_precaching: false,
    k_precached_solution_count_min: 2,
    k_precached_solution_count_max: 4,

    dont_reuse_recaptcha_solution: true,
    start_recaptcha2_solving_when_challenge_shown: false,
    set_incoming_workers_user_agent: true,
    run_explicit_invisible_hcaptcha_callback_when_challenge_shown: false,
    solve_only_presented_recaptcha2: false,

    // status
    account_key_checked: antiCaptchaPredefinedApiKey ? true : false, // set after account_key check
};
