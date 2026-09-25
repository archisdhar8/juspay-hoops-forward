const amountInput = document.querySelector("#amount");
const amountButtons = document.querySelectorAll("[data-amount]");
const campaignLinks = document.querySelectorAll("[data-select-campaign]");
const anonymousInput = document.querySelector("#anonymous");
const donorNameInput = document.querySelector("#donor-name");

if (amountInput && amountButtons.length) {
  amountButtons.forEach((button) => {
    button.addEventListener("click", () => {
      amountInput.value = button.dataset.amount;
      amountButtons.forEach((candidate) => candidate.classList.remove("selected"));
      button.classList.add("selected");
    });
  });

  amountInput.addEventListener("input", () => {
    amountButtons.forEach((candidate) => {
      candidate.classList.toggle("selected", candidate.dataset.amount === amountInput.value);
    });
  });
}

campaignLinks.forEach((link) => {
  link.addEventListener("click", () => {
    const campaignInput = document.querySelector(
      `input[name="campaign_id"][value="${link.dataset.selectCampaign}"]`
    );
    if (campaignInput) campaignInput.checked = true;
  });
});

if (anonymousInput && donorNameInput) {
  const syncDonorName = () => {
    donorNameInput.required = !anonymousInput.checked;
    donorNameInput.disabled = anonymousInput.checked;
    donorNameInput.placeholder = anonymousInput.checked
      ? "Hidden for anonymous gift"
      : "Your full name";
  };
  anonymousInput.addEventListener("change", syncDonorName);
  syncDonorName();
}
