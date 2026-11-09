const forms = {
  login: document.getElementById("loginForm"),
  signup: document.getElementById("signupForm"),
  forgot: document.getElementById("forgotForm"),
  reset: document.getElementById("resetForm"),
};
const title = document.getElementById("formTitle");
const subtitle = document.getElementById("formSubtitle");
const notice = document.getElementById("notice");
const error = document.getElementById("error");
const switchCopy = document.getElementById("switchCopy");
const backCopy = document.getElementById("backCopy");
const separator = document.getElementById("separator");
const googleButton = document.getElementById("googleButton");
const copy = {
  login: ["Sign in to your workspace", "Use your work email or username to continue."],
  signup: ["Create your account", "Sign up with your email and verify it to get started."],
  forgot: ["Reset your password", "We’ll send a one-time reset link to your email."],
  reset: ["Choose a new password", "Set a new password for your DataTrust account."],
};
let resetToken = new URLSearchParams(location.hash.slice(1)).get("reset_token")
  || new URLSearchParams(location.search).get("reset_token") || "";

function showMessage(element, text) {
  notice.hidden = true;
  error.hidden = true;
  if (!text) return;
  element.textContent = text;
  element.hidden = false;
}

function setView(view) {
  Object.entries(forms).forEach(([name, form]) => { form.hidden = name !== view; });
  title.textContent = copy[view][0];
  subtitle.textContent = copy[view][1];
  const isLogin = view === "login";
  const isPasswordFlow = view === "forgot" || view === "reset";
  separator.hidden = isPasswordFlow;
  googleButton.hidden = isPasswordFlow;
  switchCopy.hidden = !isLogin && view !== "signup";
  backCopy.hidden = isLogin || view === "signup";
  if (view === "login") {
    switchCopy.innerHTML = 'New to DataTrust? <button type="button" class="text-button" data-view="signup">Create an account</button>';
  } else if (view === "signup") {
    switchCopy.innerHTML = 'Already have an account? <button type="button" class="text-button" data-view="login">Sign in</button>';
  }
  showMessage(notice, "");
  showMessage(error, "");
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-view]");
  if (button) setView(button.dataset.view);
});

async function submitForm(form, path, payload, successView, successMessage) {
  const button = form.querySelector('button[type="submit"]');
  const label = button.querySelector("span");
  const oldLabel = label.textContent;
  button.disabled = true;
  label.textContent = "Please wait…";
  showMessage(notice, "");
  showMessage(error, "");
  try {
    const response = await fetch(`/api/auth/${path}`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || "Something went wrong. Please try again.");
    if (path === "login") {
      location.assign("/app");
      return;
    }
    if (path === "reset") {
      resetToken = "";
      history.replaceState(null, "", "/login");
      setView(successView);
    }
    showMessage(notice, body.message || successMessage);
    form.reset();
  } catch (reason) {
    showMessage(error, reason.message || "Unable to connect. Please try again.");
  } finally {
    button.disabled = false;
    label.textContent = oldLabel;
  }
}

forms.login.addEventListener("submit", (event) => {
  event.preventDefault();
  submitForm(forms.login, "login", {
    email: forms.login.email.value,
    password: forms.login.password.value,
  });
});
forms.signup.addEventListener("submit", (event) => {
  event.preventDefault();
  submitForm(forms.signup, "signup", {
    email: forms.signup.email.value,
    password: forms.signup.password.value,
  }, "login");
});
forms.forgot.addEventListener("submit", (event) => {
  event.preventDefault();
  submitForm(forms.forgot, "forgot", { email: forms.forgot.email.value }, "login");
});
forms.reset.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!resetToken) {
    showMessage(error, "This reset link is invalid. Request a new one.");
    return;
  }
  submitForm(forms.reset, "reset", {
    token: resetToken,
    password: forms.reset.password.value,
  }, "login");
});

const params = new URLSearchParams(location.search);
if (resetToken) setView("reset");
else if (params.get("error")) {
  showMessage(error, params.get("error") === "google_email"
    ? "Google did not confirm this email address. Try another sign-in method."
    : "Google sign-in could not be completed. Please try again.");
}
