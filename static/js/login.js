'use strict';

document.addEventListener('DOMContentLoaded', () => {
    const passwordInput = document.getElementById('password');
    const toggleBtn = document.querySelector('.password-toggle-btn');
    const eyeShow = document.querySelector('.eye-show');
    const eyeHide = document.querySelector('.eye-hide');
    const forgotLink = document.getElementById('forgotLink');

    if (toggleBtn && passwordInput) {
        toggleBtn.addEventListener('click', (e) => {
            e.preventDefault();
            const isPassword = passwordInput.type === 'password';
            passwordInput.type = isPassword ? 'text' : 'password';
            
            if (eyeShow && eyeHide) {
                eyeShow.style.display = isPassword ? 'none' : 'block';
                eyeHide.style.display = isPassword ? 'block' : 'none';
            }
            toggleBtn.setAttribute('aria-label', isPassword ? 'Hide password' : 'Show password');
        });
    }

    if (forgotLink) {
        forgotLink.addEventListener('click', (e) => {
            e.preventDefault();
            alert('Default System Access Credentials:\n\n• Username: user\n• Password: pass123\n\nOr click "Sign in as Guest" for instant access.');
        });
    }
});
