const amountInput = document.querySelector("#amount");
const amountButtons = document.querySelectorAll("[data-amount]");

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
